#!/bin/bash

# This file is both an integration test that runs once a day on a v4-16 and documentation for how to get started with Gemma-7b. 
# Please make sure you have run end_to_end/tpu/gemma/7b/1_test_gemma.sh before running commands from this file. 

# The flow of this file is as follows:
# 1. Run decoding, finetuning of Gemma 7B with the converted checkpoint obtained from end_to_end/tpu/gemma/7b/1_test_gemma.sh. Also, run pretraining of Gemma 7B
# 2. Convert the scanned checkpoint from step 1 into unscanned checkpoint format and run more efficient decoding.
# 3. Run decoding from the finetuned checkpoint from step 1
# 4. Ahead of Time Compilation for running Gemma 7B on v5e-256

# Example Usage: export BASE_OUTPUT_PATH=/path/to/GCS/bucket; bash end_to_end/tpu/gemma/7b/1_test_gemma.sh
# Use the same BASE_OUTPUT_PATH as end_to_end/tpu/gemma/7b/1_test_gemma.sh
# Please note that in these two scripts (1_test_gemma.sh and 2_test_gemma.sh) BASE_OUTPUT_PATH is assumed to be already a unique path across multiple runs and 
# the subfolders names aka RUN_NAMEs are static. Please remember to change BASE_OUTPUT_PATH across different runs.

set -ex
export MODEL_VARIATION='7b'

if [ -z "${BASE_OUTPUT_PATH}" ]; then
    # Non-Googlers please remember to point `BASE_OUTPUT_PATH` to a GCS bucket that you own, this bucket will store all the files generated by MaxText during a run
    # Use the same BASE_OUTPUT_PATH as end_to_end/tpu/gemma/7b/1_test_gemma.sh
    export BASE_OUTPUT_PATH=gs://runner-maxtext-logs/$(date +%Y-%m-%d-%H-%M)
    echo "BASE_OUTPUT_PATH is not set, using BASE_OUTPUT_PATH = ${BASE_OUTPUT_PATH}"
fi



# Non-Googlers please remember to point `DATASET_PATH` to the GCS bucket where you have your training data
export DATASET_PATH=gs://maxtext-dataset


# We define `CONVERTED_CHECKPOINT` to refer to the checkpoint subdirectory. This way it is easier to use this path in the `train.py` and `decode.py` commands
export CONVERTED_CHECKPOINT=${BASE_OUTPUT_PATH}/${MODEL_VARIATION}/scanned_chkpt/0/items
export RUN_NAME=unscanned_chkpt
# We defined path to unscanned checkpoint created in 1_test_gemma.sh
export UNSCANNED_CKPT_PATH=${BASE_OUTPUT_PATH}/${RUN_NAME}/checkpoints/0/items

export ASYNC_CHECKPOINTING=True # True so that the jax distributed system is initialized
# We run decoding on the `UNSCANNED_CKPT_PATH` for efficient decoding on the unscanned version of the checkpoint. Note that this checkpoint only has parameters and no optimizer state. 
# So, we use it by specifying`load_parameters_path=${CONVERTED_CHECKPOINT}`
python MaxText/decode.py MaxText/configs/base.yml base_output_directory=gs://runner-maxtext-logs tokenizer_path=assets/tokenizer.gemma load_parameters_path=${UNSCANNED_CKPT_PATH} per_device_batch_size=1 run_name=runner_$(date +%Y-%m-%d-%H-%M) max_prefill_predict_length=4 max_target_length=16 dataset_type=synthetic steps=10 async_checkpointing=${ASYNC_CHECKPOINTING} scan_layers=false model_name=gemma-7b attention=dot_product prompt="I love to"

# We can also run decoding (albeit in a bit unoptimized way) by using the scanned converted checkpoint located at `CONVERTED_CHECKPOINT`. Note again that this checkpoint only has parameters and no optimizer state. So, we use it by specifying`load_parameters_path=${CONVERTED_CHECKPOINT}`
python MaxText/decode.py MaxText/configs/base.yml base_output_directory=gs://runner-maxtext-logs tokenizer_path=assets/tokenizer.gemma load_parameters_path=${CONVERTED_CHECKPOINT} per_device_batch_size=1 run_name=runner_$(date +%Y-%m-%d-%H-%M) max_prefill_predict_length=4 max_target_length=16 dataset_type=synthetic steps=10 async_checkpointing=${ASYNC_CHECKPOINTING} model_name=gemma-7b attention=dot_product prompt="I love to"

# Alternatively, we skip to running finetuning by using the scanned converted checkpoint located at `CONVERTED_CHECKPOINT`. Again, we use it by specifying`load_parameters_path=${CONVERTED_CHECKPOINT}`. Note that scanned checkpoint helps with efficient finetuning
export FINETUNE_RUN_NAME=runner_finetune
python MaxText/train.py MaxText/configs/base.yml base_output_directory=${BASE_OUTPUT_PATH} dataset_path=${DATASET_PATH} tokenizer_path=assets/tokenizer.gemma load_parameters_path=${CONVERTED_CHECKPOINT} per_device_batch_size=1 run_name=${FINETUNE_RUN_NAME} max_target_length=8192 steps=10 async_checkpointing=${ASYNC_CHECKPOINTING} model_name=gemma-7b checkpoint_period=5

# We also run pre-training, this is similar to the finetuning command except we don't pass any checkpoint directory to load parameters from
python MaxText/train.py MaxText/configs/base.yml base_output_directory=${BASE_OUTPUT_PATH} dataset_path=${DATASET_PATH} tokenizer_path=assets/tokenizer.gemma per_device_batch_size=1 run_name=runner_$(date +%Y-%m-%d-%H-%M) max_target_length=8192 steps=5 enable_checkpointing=false model_name=gemma-7b

# Note that the finetune run checkpoint generates the `full state` which has both parameters and optimizer state. For decoding, we only need to use the parameters. 
# So, we can use the `MaxText/generate_param_only_checkpoint.py` to convert the full state checkpoint into a parameter only checkpoint for more efficient memory use. Note that the path provided to the flag `load_full_state_path` is the path to the checkpoint subdirectory inside the `BASE_OUTPUT_PATH` from our previous finetuning run.
# `force_unroll=true` is converting the output parameter only checkpoint into an unscanned format for efficient decoding
export PARAM_RUN_NAME=param_chkpt
python MaxText/generate_param_only_checkpoint.py MaxText/configs/base.yml base_output_directory=${BASE_OUTPUT_PATH} load_full_state_path=${BASE_OUTPUT_PATH}/${FINETUNE_RUN_NAME}/checkpoints/5/items run_name=${PARAM_RUN_NAME} model_name='gemma-7b' force_unroll=true

# Now, run decoding on the checkpoint generated from our finetune run.
python MaxText/decode.py MaxText/configs/base.yml base_output_directory=gs://runner-maxtext-logs tokenizer_path=assets/tokenizer.gemma load_parameters_path=${BASE_OUTPUT_PATH}/${PARAM_RUN_NAME}/checkpoints/0/items per_device_batch_size=1 run_name=runner_$(date +%Y-%m-%d-%H-%M) max_prefill_predict_length=4 max_target_length=16 dataset_type=synthetic steps=10 async_checkpointing=${ASYNC_CHECKPOINTING} scan_layers=false model_name=gemma-7b attention=dot_product prompt="I love to"

# We recommend training/finetuning Gemma on v5e-256 using the following sharding strategy to achieve optimal performance.
# This below command does Ahead Of Time Cross Compilation (https://github.com/google/maxtext?tab=readme-ov-file#ahead-of-time-compilation-aot) for our recommended v5e-256 configuration for Gemma 7B.
# To actually run it on real v5e-256's simple replace the train_compile.py with a train.py and get rid of compile_topology args.
python MaxText/train_compile.py MaxText/configs/base.yml model_name=gemma-7b ici_fsdp_transpose_parallelism=16 per_device_batch_size=2 compile_topology=v5e-256 compile_topology_num_slices=1
