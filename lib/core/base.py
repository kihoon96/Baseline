import os.path as osp
import numpy as np
from tqdm import tqdm
import json
import torch
import torch.nn as nn
import torchvision.transforms as transforms
from torch.utils.data import DataLoader
from collections import Counter

from core.config import cfg
from core.logger import logger

import Human36M.dataset
from models import get_model, transfer_backbone
from multiple_datasets import MultipleDatasets
from core.loss import get_loss
from coord_utils import heatmap_to_coords
from funcs_utils import get_optimizer, load_checkpoint, get_scheduler, count_parameters
from eval_utils import eval_mpjpe, eval_pa_mpjpe, eval_2d_joint_accuracy
from vis_utils import save_plot
from human_models import smpl


def get_dataloader(dataset_names, is_train):
    if len(dataset_names) == 0: return None, None

    dataset_split = 'TRAIN' if is_train else 'TEST'  
    batch_per_dataset = cfg[dataset_split].batch_size // len(dataset_names)
    dataset_list, dataloader_list = [], []

    logger.info(f"==> Preparing {dataset_split} Dataloader...")
    for name in dataset_names:
        transform = transforms.Compose([
                        transforms.ToTensor(),
                        transforms.Normalize(mean=[0.485, 0.456, 0.406], std=[0.229, 0.224, 0.225])])
        
        dataset = eval(f'{name}.dataset')(transform, dataset_split.lower())
        logger.info(f"# of {dataset_split} {name} data: {len(dataset)}")
        dataset_list.append(dataset)

    if not is_train:
        for dataset in dataset_list:
            dataloader = DataLoader(dataset,
                                batch_size=batch_per_dataset,
                                shuffle=cfg[dataset_split].shuffle,
                                num_workers=cfg.DATASET.workers,
                                pin_memory=True,
                                drop_last=False)
            dataloader_list.append(dataloader)
        
        return dataset_list, dataloader_list
    else:
        trainset_loader = MultipleDatasets(dataset_list, partition=cfg.DATASET.train_partition, make_same_len=cfg.DATASET.make_same_len)
        batch_generator = DataLoader(dataset=trainset_loader, batch_size=batch_per_dataset * len(dataset_names), shuffle=cfg[dataset_split].shuffle,
                                     num_workers=cfg.DATASET.workers, pin_memory=True, drop_last=True)
        return dataset_list, batch_generator


def prepare_network(args, load_dir='', is_train=True):    
    model, checkpoint = None, None
    
    model = get_model(is_train)
    logger.info(f"==> Constructing Model...")
    logger.info(f"# of model parameters: {count_parameters(model)}")
    logger.info(model)
    
    if load_dir and (not is_train or args.resume_training):
        logger.info(f"==> Loading checkpoint: {load_dir}")
        checkpoint = load_checkpoint(load_dir=load_dir)
        model.load_state_dict(checkpoint['model_state_dict'])

    return model, checkpoint


def train_setup(model, checkpoint):    
    criterion, optimizer, lr_scheduler = None, None, None
    loss_history = {'total_loss': [], 'joint_loss': [], 'smpl_joint_loss': [], 'proj_loss': [], 'pose_param_loss': [], 'shape_param_loss': [], 'prior_loss': []}
    error_history = {'mpjpe': [], 'pa_mpjpe': [], 'mpvpe': []}
    
    criterion = get_loss()
    optimizer = get_optimizer(model=model)
    lr_scheduler = get_scheduler(optimizer=optimizer)
    
    if checkpoint is not None:
        optimizer.load_state_dict(checkpoint['optim_state_dict'])
        for state in optimizer.state.values():
            for k, v in state.items():
                if torch.is_tensor(v):
                    state[k] = v.cuda()
        curr_lr = 0.0

        for param_group in optimizer.param_groups:
            curr_lr = param_group['lr']

        lr_state = checkpoint['scheduler_state_dict']
        lr_state['milestones'], lr_state['gamma'] = Counter(cfg.TRAIN.lr_step), cfg.TRAIN.lr_factor
        lr_scheduler.load_state_dict(lr_state)

        loss_history = checkpoint['train_log']
        error_history = checkpoint['test_log']
        cfg.TRAIN.begin_epoch = checkpoint['epoch'] + 1
        logger.info("===> resume from epoch {:d}, current lr: {:.0e}, milestones: {}, lr factor: {:.0e}"
                    .format(cfg.TRAIN.begin_epoch, curr_lr, lr_state['milestones'], lr_state['gamma']))

    return criterion, optimizer, lr_scheduler, loss_history, error_history
    

class Trainer:
    def __init__(self, args, load_dir):
        self.model, checkpoint = prepare_network(args, load_dir, True)
        self.loss, self.optimizer, self.lr_scheduler, self.loss_history, self.error_history = train_setup(self.model, checkpoint)
        dataset_list, self.batch_generator = get_dataloader(cfg.DATASET.train_list, is_train=True)
        
        self.model = self.model.cuda()
        self.model = nn.DataParallel(self.model) 
        self.print_freq = cfg.TRAIN.print_freq
        
        self.joint_loss_weight = cfg.TRAIN.joint_loss_weight
        self.proj_loss_weight = cfg.TRAIN.proj_loss_weight
        self.pose_loss_weight = cfg.TRAIN.pose_loss_weight
        self.shape_loss_weight = cfg.TRAIN.shape_loss_weight
        self.prior_loss_weight = cfg.TRAIN.prior_loss_weight

        
    def train(self, epoch):
        self.model.train()
        lr = self.lr_scheduler.get_last_lr()[0]

        running_loss = 0.0
        running_joint_loss = 0.0
        running_smpl_joint_loss = 0.0
        running_proj_loss = 0.0
        running_pose_param_loss = 0.0
        running_shape_param_loss = 0.0
        running_prior_loss = 0.0
        
        batch_generator = tqdm(self.batch_generator)
        for i, batch in enumerate(batch_generator):
            inp_img = batch['img'].cuda()
            tar_joint_img, tar_joint_cam, tar_smpl_joint_cam = batch['joint_img'].cuda(), batch['joint_cam'].cuda(), batch['smpl_joint_cam'].cuda()
            tar_pose, tar_shape = batch['pose'].cuda(), batch['shape'].cuda()
            meta_joint_valid, meta_has_3D, meta_has_param = batch['joint_valid'].cuda(), batch['has_3D'].cuda(), batch['has_param'].cuda()
            
            pred_mesh_cam, pred_joint_cam, pred_joint_proj, pred_smpl_pose, pred_smpl_shape = self.model(inp_img)

            loss1 = self.joint_loss_weight * self.loss['joint_cam'](pred_joint_cam, tar_joint_cam, meta_joint_valid * meta_has_3D)
            loss2 = self.joint_loss_weight * self.loss['smpl_joint_cam'](pred_joint_cam, tar_smpl_joint_cam, meta_has_param[:,:,None])
            loss3 = self.proj_loss_weight * self.loss['joint_proj'](pred_joint_proj, tar_joint_img, meta_joint_valid)
            loss4 = self.pose_loss_weight * self.loss['pose_param'](pred_smpl_pose, tar_pose, meta_has_param)
            loss5 = self.shape_loss_weight * self.loss['shape_param'](pred_smpl_shape, tar_shape, meta_has_param)
            loss6 = self.prior_loss_weight * self.loss['prior'](pred_smpl_pose[:,3:], pred_smpl_shape)
            loss = loss1 + loss2 + loss3 + loss4 + loss5 + loss6
            
            # update weights
            self.optimizer.zero_grad()
            loss.backward()
            self.optimizer.step()

            # log
            loss, loss1, loss2, loss3, loss4, loss5, loss6 = loss.detach(), loss1.detach(), loss2.detach(), loss3.detach(), loss4.detach(), loss5.detach(), loss6.detach()
            running_loss += float(loss.item())
            running_joint_loss += float(loss1.item())
            running_smpl_joint_loss += float(loss2.item())
            running_proj_loss += float(loss3.item())
            running_pose_param_loss += float(loss4.item())
            running_shape_param_loss += float(loss5.item())
            running_prior_loss += float(loss6.item())
            
            if i % self.print_freq == 0:
                batch_generator.set_description(f'Epoch{epoch} ({i}/{len(batch_generator)}) => '
                                                f'joint: {loss1:.4f} smpl_joint: {loss2:.4f} proj: {loss3:.4f} pose: {loss4:.4f}, shape: {loss5:.4f}, prior: {loss6:.4f}')

        self.loss_history['total_loss'].append(running_loss / len(batch_generator)) 
        self.loss_history['joint_loss'].append(running_joint_loss / len(batch_generator))     
        self.loss_history['smpl_joint_loss'].append(running_smpl_joint_loss / len(batch_generator))     
        self.loss_history['proj_loss'].append(running_proj_loss / len(batch_generator)) 
        self.loss_history['pose_param_loss'].append(running_pose_param_loss / len(batch_generator)) 
        self.loss_history['shape_param_loss'].append(running_shape_param_loss / len(batch_generator)) 
        self.loss_history['prior_loss'].append(running_prior_loss / len(batch_generator)) 
        
        logger.info(f'Epoch{epoch} Loss: {self.loss_history["total_loss"][-1]:.4f}')


class Tester:
    def __init__(self, args, load_dir=''):
        if load_dir != '':
            self.model, _ = prepare_network(args, load_dir, False)
            self.model = self.model.cuda()
            self.model = nn.DataParallel(self.model)

        dataset_list, self.val_loader = get_dataloader(cfg.DATASET.test_list, is_train=False)
        if dataset_list is not None:
            self.val_dataset = dataset_list[0]
            self.val_loader = self.val_loader[0]
            self.dataset_length = len(self.val_dataset)
            
            if self.val_dataset.joint_set['name'] == '3DPW':
                self.eval_mpvpe = True
            else:
                self.eval_mpvpe = False
        
        self.J_regressor = torch.from_numpy(smpl.h36m_joint_regressor).float().cuda()

        self.print_freq = cfg.TRAIN.print_freq
        self.vis_freq = cfg.TEST.vis_freq
        
        self.mpjpe = 9999
        self.pa_mpjpe = 9999
        self.mpvpe = 9999
            
    def test(self, epoch, current_model=None):
        if current_model:
            self.model = current_model
        self.model.eval()
        
        mpjpe, pa_mpjpe, mpvpe = [], [], []
        
        eval_prefix = f'Epoch{epoch} ' if epoch else ''
        loader = tqdm(self.val_loader)
        with torch.no_grad():
            for i, batch in enumerate(loader):
                inp_img = batch['img'].cuda()
                batch_size = inp_img.shape[0]

                # feed-forward
                pred_mesh_cam, pred_joint_cam, pred_joint_proj, pred_smpl_pose, pred_smpl_shape = self.model(inp_img)
                # meter to milimeter
                pred_mesh_cam, pred_joint_cam = pred_mesh_cam * 1000, pred_joint_cam * 1000

                # eval post processing
                pred_joint_cam = torch.matmul(self.J_regressor[None, :, :], pred_mesh_cam)
                pred_joint_cam = pred_joint_cam.cpu().numpy()
                tar_joint_cam = batch['joint_cam'].cpu().numpy()
                pred_mesh_cam = pred_mesh_cam.cpu().numpy()
                tar_mesh_cam = batch['mesh_cam'].cpu().numpy()
                
                mpjpe_i, pa_mpjpe_i = self.eval_3d_joint(pred_joint_cam, tar_joint_cam)
                mpjpe.extend(mpjpe_i); pa_mpjpe.extend(pa_mpjpe_i)
                mpjpe_i, pa_mpjpe_i = sum(mpjpe_i)/batch_size, sum(pa_mpjpe_i)/batch_size
                
                if self.eval_mpvpe:
                    mpvpe_i = self.eval_mesh(pred_mesh_cam, tar_mesh_cam, pred_joint_cam, tar_joint_cam)
                    mpvpe.extend(mpvpe_i)
                    mpvpe_i = sum(mpvpe_i)/batch_size
                                
                if i % self.print_freq == 0:
                    if self.eval_mpvpe:
                        loader.set_description(f'{eval_prefix}({i}/{len(self.val_loader)}) => MPJPE: {mpjpe_i:.2f}, PA-MPJPE: {pa_mpjpe_i:.2f} MPVPE: {mpvpe_i:.2f}')
                    else:
                        loader.set_description(f'{eval_prefix}({i}/{len(self.val_loader)}) => MPJPE: {mpjpe_i:.2f}, PA-MPJPE: {pa_mpjpe_i:.2f}')
                    
                if cfg.TEST.vis:
                    import cv2
                    from vis_utils import vis_3d_pose, save_obj
                    
                    if i % self.vis_freq == 0:
                        inv_normalize = transforms.Normalize(mean=[-0.485/0.229, -0.456/0.224, -0.406/0.225], std=[1/0.229, 1/0.224, 1/0.225])
                        img = inv_normalize(inp_img[0]).cpu().numpy().transpose(1,2,0)[:,:,::-1]
                        img = np.ascontiguousarray(img, dtype=np.uint8)
                        cv2.imwrite(osp.join(cfg.vis_dir, f'test_{i}_img.png'), img)
                        
                        vis_3d_pose(pred_joint_cam[0], smpl.h36m_skeleton, 'human36', osp.join(cfg.vis_dir, f'test_{i}_joint_cam_pred.png'))
                        vis_3d_pose(tar_joint_cam[0], smpl.h36m_skeleton, 'human36', osp.join(cfg.vis_dir, f'test_{i}_joint_cam_gt.png'))
                        
                        save_obj(pred_mesh_cam[0], smpl.face, osp.join(cfg.vis_dir, f'test_{i}_mesh_cam_pred.obj'))
                        if self.eval_mpvpe: save_obj(tar_mesh_cam[0], smpl.face, osp.join(cfg.vis_dir, f'test_{i}_mesh_cam_gt.obj'))
                       
            self.mpjpe = sum(mpjpe) / self.dataset_length
            self.pa_mpjpe = sum(pa_mpjpe) / self.dataset_length
            self.mpvpe = sum(mpvpe) / self.dataset_length
            
            if self.eval_mpvpe:
                logger.info(f'>> {eval_prefix} MPJPE: {self.mpjpe:.2f}, PA-MPJPE: {self.pa_mpjpe:.2f} MPVPE: {self.mpvpe:.2f}')
            else:
                logger.info(f'>> {eval_prefix} MPJPE: {self.mpjpe:.2f}, PA-MPJPE: {self.pa_mpjpe:.2f}')

    def save_history(self, loss_history, error_history, epoch):
        error_history['mpjpe'].append(self.mpjpe)
        error_history['pa_mpjpe'].append(self.pa_mpjpe)
        error_history['mpvpe'].append(self.mpvpe)

        save_plot(error_history['mpjpe'], epoch, title='MPJPE', show_min=True)
        save_plot(error_history['pa_mpjpe'], epoch, title='PA-MPJPE', show_min=True)
        save_plot(error_history['mpvpe'], epoch, title='MPVPE', show_min=True)
        
        save_plot(loss_history['joint_loss'], epoch, title='Joint Loss')
        save_plot(loss_history['smpl_joint_loss'], epoch, title='SMPL Joint Loss')
        save_plot(loss_history['proj_loss'], epoch, title='Joint Proj Loss')
        save_plot(loss_history['pose_param_loss'], epoch, title='Pose Param Loss')
        save_plot(loss_history['shape_param_loss'], epoch, title='Shape Param Loss')
        save_plot(loss_history['prior_loss'], epoch, title='Prior Loss')
        
        save_plot(loss_history['total_loss'], epoch, title='Total Loss')

    def eval_3d_joint(self, pred, target):
        pred, target = pred.copy(), target.copy()
        batch_size = pred.shape[0]
        
        pred, target = pred - pred[:, None, smpl.h36m_root_joint_idx, :], target - target[:, None, smpl.h36m_root_joint_idx, :]
        pred, target = pred[:, smpl.h36m_eval_joints, :], target[:, smpl.h36m_eval_joints, :]
        
        mpjpe, pa_mpjpe = [], []
        for j in range(batch_size):
            mpjpe.append(eval_mpjpe(pred[j], target[j]))
            pa_mpjpe.append(eval_pa_mpjpe(pred[j], target[j]))
        
        return mpjpe, pa_mpjpe
    
    
    def eval_mesh(self, pred, target, pred_joint_cam, gt_joint_cam):
        pred, target = pred.copy(), target.copy()
        batch_size = pred.shape[0]
        
        pred, target = pred - pred_joint_cam[:, None, smpl.h36m_root_joint_idx, :], target - gt_joint_cam[:, None, smpl.h36m_root_joint_idx, :]
        pred, target = pred[:, smpl.h36m_eval_joints, :], target[:, smpl.h36m_eval_joints, :]
        
        mpvpe = []
        for j in range(batch_size):
            mpvpe.append(eval_mpjpe(pred[j], target[j]))
        
        return mpvpe