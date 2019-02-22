# Soft

Za pokretanje, pokrenuti main.py sa narednim argumentima:
``` bash
python3 src/main.py \
        --train_data_dir ./data/training/ \
        --save_path=./runs \
        --logdir=./logdir \
        --num_epoch=25 \
        --batch_size=1 \
        --patch_size=388 \
        --gpu=0 \
        --eval_every=1000 \
        --stride=12 \
        --train_score_every=10000 \
        --image_augmentation \
        --rotation_angles 15,30,45,60,75 \
        --ensemble_prediction \
        --dilated_layers \
        --num_layers=6 \
        --dropout=1.0
```
