#!/bin/bash

# This file is both an integration test that runs once a day on a v4-16 and documentation for how to get started with Gemma2-9b. 
# Please make sure you have run end_to_end/tpu/gemma2/9b/1_test_gemma.sh before running commands from this file. 

# The flow of this file is as follows:
# 1. Run decoding, finetuning of Gemma2 9b with the converted checkpoint obtained from end_to_end/tpu/gemma2/9b/1_test_gemma.sh. Also, run pretraining of Gemma2 9b
# 2. Convert the scanned checkpoint from step 1 into unscanned checkpoint format and run more efficient decoding.
# 3. Run decoding from the finetuned checkpoint from step 1
# 4. Ahead of Time Compilation for running Gemma2 9b on v5e-256

# Example Usage: export BASE_OUTPUT_PATH=/path/to/GCS/bucket; bash end_to_end/tpu/gemma2/9b/1_test_gemma.sh
# Use the same BASE_OUTPUT_PATH as end_to_end/tpu/gemma2/9b/1_test_gemma.sh
# Please note that in these two scripts (1_test_gemma.sh and 2_test_gemma.sh) BASE_OUTPUT_PATH is assumed to be already a unique path across multiple runs and 
# the subfolders names aka RUN_NAMEs are static. Please remember to change BASE_OUTPUT_PATH across different runs.

set -ex
export MODEL_VARIATION='9b'

# Installing torch for deps in forward_pass_logit_chekcker.py
pip install torch --index-url https://download.pytorch.org/whl/cpu

if [ -z "${BASE_OUTPUT_PATH}" ]; then
    # Non-Googlers please remember to point `BASE_OUTPUT_PATH` to a GCS bucket that you own, this bucket will store all the files generated by MaxText during a run
    # Use the same BASE_OUTPUT_PATH as end_to_end/tpu/gemma2/9b/1_test_gemma.sh
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

# We run decoding on the `UNSCANNED_CKPT_PATH` for efficient decoding on the unscanned version of the checkpoint. Note that this checkpoint only has parameters and no optimizer state. 
# So, we use it by specifying`load_parameters_path=${CONVERTED_CHECKPOINT}`
python MaxText/decode.py MaxText/configs/base.yml tokenizer_path=assets/tokenizer.gemma load_parameters_path=${UNSCANNED_CKPT_PATH} per_device_batch_size=1 run_name=runner_$(date +%Y-%m-%d-%H-%M) max_prefill_predict_length=8 max_target_length=16 dataset_type=synthetic steps=10 async_checkpointing=false scan_layers=false model_name=gemma2-9b attention=dot_product prompt="I love to"

# We can also run decoding (albeit in a bit unoptimized way) by using the scanned converted checkpoint located at `CONVERTED_CHECKPOINT`. Note again that this checkpoint only has parameters and no optimizer state. So, we use it by specifying`load_parameters_path=${CONVERTED_CHECKPOINT}`
python MaxText/decode.py MaxText/configs/base.yml tokenizer_path=assets/tokenizer.gemma load_parameters_path=${CONVERTED_CHECKPOINT} per_device_batch_size=1 run_name=runner_$(date +%Y-%m-%d-%H-%M) max_prefill_predict_length=8 max_target_length=16 dataset_type=synthetic steps=10 async_checkpointing=false model_name=gemma2-9b attention=dot_product prompt="I love to"

# We also test whether the forward pass logits match the golden logits for Gemma2-9b
# to get higher precision (eg. float32) run on CPU with `JAX_PLATFORMS=cpu`
python3 MaxText/tests/forward_pass_logit_checker.py  MaxText/configs/base.yml tokenizer_path=assets/tokenizer.gemma load_parameters_path=${UNSCANNED_CKPT_PATH} run_name=forward_pass_test_gemma2_9b per_device_batch_size=1 model_name=gemma2-9b max_prefill_predict_length=4 max_target_length=4 dataset_type=synthetic scan_layers=false dtype='float32' --atol=1.0 --rtol=1.0 --max_kl_div=0.15