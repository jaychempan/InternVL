set -x

PARTITION=${PARTITION:-"INTERN2"}
GPUS=${GPUS:-512}
GPUS_PER_NODE=${GPUS_PER_NODE:-8}
QUOTA_TYPE=${QUOTA_TYPE:-"reserved"}
NODES=$((GPUS / GPUS_PER_NODE))
CPUS_PER_TASK=${CPUS_PER_TASK:-1}
SRUN_ARGS=${SRUN_ARGS:-""}
BATCH_SIZE=${BATCH_SIZE:-4096}
PER_DEVICE_BATCH_SIZE=${PER_DEVICE_BATCH_SIZE:-8}
GRADIENT_ACC=$((BATCH_SIZE / PER_DEVICE_BATCH_SIZE / GPUS))

export PYTHONPATH="/mnt/petrelfs/wangweiyun/workspace_cz/InternVL/internvl_chat_dev/petrel-oss-python-sdk"
export PYTHONPATH="${PYTHONPATH}:$(pwd)"
export MASTER_PORT=34227
export TF_CPP_MIN_LOG_LEVEL=3

OUTPUT_DIR='work_dirs/interleaved/internvl_chat_v1_5_llama3_8b_dynamic_res_pretrain_interleaved'

if [ ! -d "$OUTPUT_DIR" ]; then
  mkdir -p "$OUTPUT_DIR"
fi

# number of gpus: 512
# batch size per gpu: 8
# gradient accumulation steps: 1
# total batch size: 2048
# epoch: 1
srun -p ${PARTITION} \
  --gres=gpu:${GPUS_PER_NODE} \
  --nodes=${NODES} \
  --ntasks=${GPUS} \
  --ntasks-per-node=${GPUS_PER_NODE} \
  --cpus-per-task=${CPUS_PER_TASK} \
  --kill-on-bad-exit=1 \
  --quotatype=${QUOTA_TYPE} \
  ${SRUN_ARGS} \
  python -u internvl/train/internvl_chat_pretrain_interleaved.py \
  --vision_path "./pretrained/intern_vit_300m_448px_v1_5" \
  --llm_path "./pretrained/Meta-Llama-3-8B-Add-Token" \
  --conv_style "llama3-chat" \
  --output_dir ${OUTPUT_DIR} \
  --meta_path "./shell/data/data_0404_zh_pretrain_v3.json" \
  --overwrite_output_dir True \
  --force_image_size 448 \
  --max_dynamic_patch 5 \
  --down_sample_ratio 0.5 \
  --drop_path_rate 0.0 \
  --pad2square False \
  --freeze_llm False \
  --freeze_mlp False \
  --freeze_backbone True \
  --vision_select_layer -1 \
  --use_data_resampling False \
  --dataloader_num_workers 8 \
  --bf16 True \
  --num_train_epochs 1 \
  --per_device_train_batch_size ${PER_DEVICE_BATCH_SIZE} \
  --gradient_accumulation_steps ${GRADIENT_ACC} \
  --evaluation_strategy "no" \
  --save_strategy "steps" \
  --save_steps 100 \
  --save_total_limit 5 \
  --learning_rate 2e-5 \
  --weight_decay 0.05 \
  --warmup_steps 1000 \
  --lr_scheduler_type "cosine" \
  --logging_steps 1 \
  --max_seq_length 2048 \
  --do_train True \
  --grad_checkpoint True \
  --group_by_length False \
  --dynamic_image_size True \
  --use_thumbnail True \
  --ps_version 'v2' \
  --deepspeed "zero_stage1_config.json" \
  --report_to "tensorboard" \
  2>&1 | tee -a "${OUTPUT_DIR}/training_log.txt"
