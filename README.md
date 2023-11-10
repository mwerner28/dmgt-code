
## Overview
Given any stream of data, any assessment of its value, and any formulation of its selection cost, our method extracts the most valuable subset of the stream up to a constant factor in an online fashion. The procedure is simple (selecting each point if its marginal value given the currently selected set exceeds a threshold decided by the analyst at that time) and memory-efficient (storing only the selected subset in memory). We provide algorithms for the multi-agent distributed setting. 

## Usage
You can reproduce the experiments in our paper by running:
```
git clone ...
conda env create -f environment.yml
conda activate dmgt
python <filename.py> --dataset_name 'imagenet(or mnist)' --train_path 'path/to/imagenet(or mnist)/train/' --val_path 'path/to/imagenet(or mnist)/val/'
```
