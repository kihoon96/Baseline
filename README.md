# Baseline Code for HPS estimation

## Data configuration
- The `data` directory is be composed as below.

```
${data}
|---base_data
|---Human36M
|---|---annotations
|---|---images
```

- Download it below and replace `data/base_data` and `experiment`.

[[Link](https://drive.google.com/drive/folders/1saKaSF4nfUYS8eqZLbDmRHhEKSQ9vwu7?usp=sharing)]


## Requirement
- torch==1.7.0
- torchvision==0.8.1


## Run

Training

```
python main/train.py --gpu 0,1 --cfg ./asset/yaml/train_example.yml
```

Evaluation

```
python main/test.py --gpu 0 --cfg ./asset/yaml/eval_example.yml
```