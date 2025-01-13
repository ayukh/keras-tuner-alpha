import keras
import jax 
import numpy as np 
from abc import ABC, abstractmethod
from typing import Optional, Any, List
from keras.src.backend.common import global_state
from keras.distribution import set_distribution
from kithara.distributed.sharding import ShardingStrategy
from kithara.distributed.sharding.utils import (
    print_elements_that_are_unsharded_and_large_in_pytree,
)
from keras.src.backend.common import global_state
from kithara.distributed.sharding._mesh import Axis
from jax.experimental import multihost_utils
import time

class ModelValidationMixin:
    """Mixin providing common model validation functionality."""

    def validate_sharding(self, model: Any) -> None:
        if model is None:
            raise ValueError("Model has not been successfully created.")
        print_elements_that_are_unsharded_and_large_in_pytree(model)


class Model(ABC, ModelValidationMixin):
    """
    Base class for all models in Kithara. This class serves as a thin
    wrapper around the underlying model instance, providing a uniform
    interface for Kithara workloads. Currently supported underlying model
    implementations include MaxText and KerasHub models.

    Attributes:
        sharding_strategy(kithara.ShardingStrategy): Strategy used for 
            distributing model, optimizer, and data tensors. 
            E.g. `kithara.PredefinedShardingStrategy("fsdp", "gemma")`.
        model(Keras.Model): The underlying Keras model instance.
        model_name(str, optional): Optional name of the model.
        precision(str, optional): Optional mixed-precision policy for 
            model weights and activations.
            Default is "mixed_bfloat16". Supported policies include 
            "float32", "float16", "bfloat16", "mixed_float16", and "mixed_bfloat16". 
            Mixed precision policies load model weight in float32 and casts 
            activations to the specified dtype.
        scan_layers: Boolean indicating whether to scan layers using 
            jax.lax.scan, which speeds up training compilation. 
            Currently only MaxText models support this feature.
        lora_rank: Int indicating the rank of the LoRA weights. Currently
            only KerasHub models support LoRA. KerasHub models apply LoRA
            to the v_proj and q_proj weights. 
    Key Methods:
        __init__():
            Initializes the Model instance with the given parameters.
        __getattr__():
            Delegates any unknown attributes/methods to the underlying model.
        generate():
            Generate text tokens using the model based on the input prompt. 
        stateless_call():
            Runs the forward pass of the model in a stateless fashion. This
            function is handled by keras.model.stateless_call().
    """

    def __init__(
        self,
        model: keras.Model,
        sharding_strategy: ShardingStrategy,
        model_name: str =None,
        precision: str = "mixed_bfloat16",
        scan_layers: bool =False,
        lora_rank: int = None,
    ):

        self.sharding_strategy = sharding_strategy
        self.model = model
        self.scan_layers = scan_layers
        self.model_name = model_name
        self.precision = precision
        self.lora_rank = lora_rank
        self.weight_dtype = self._weight_dtype(precision)
        self.activation_dtype = self._activation_dtype(precision)

    def __getattr__(self, name):
        try:
            # Try to get the attribute from the Model class first
            return object.__getattribute__(self, name)
        except AttributeError:
            # If not found, delegate to _model
            model = object.__getattribute__(self, "model")
            return getattr(model, name, None)

    @staticmethod
    def _weight_dtype(precision: Optional[str] = None) -> str:
        if "mixed" in precision:
            return "float32"
        return precision

    @staticmethod
    def _activation_dtype(precision: Optional[str] = None) -> str:
        if "mixed" in precision:
            return precision.split("_")[1]
        return precision
    
    @abstractmethod
    def save_in_hf_format(self, output_dir: str, dtype: str = "auto", parallel_threads=8):
        """Save the model in HuggingFace format.

        Args:
            output_dir (str): Directory path where the model should be saved.
                Directory could be local or a Google cloud storage path, and
                will be created if it doesn't exist.
            dtype (str, optional): Data type for saved weights. Defaults to "auto".
            parallel_threads (int, optional): Number of parallel threads to use for saving.
        """

    def make_generate_step(self):
        """Create a JIT-compiled function for single-step token generation.
        
        Returns:
            function: Compiled function that performs one step of token generation.
        """
        def fn(trainable_variables, non_trainable_variables, x):
            logits, non_trainable_variables = self.model.stateless_call(
                trainable_variables, non_trainable_variables, x
            )
            return logits
        return jax.jit(fn)

    def generate(self,
                inputs,
                max_length=None,
                stop_token_ids=None,
                strip_prompt=False):
        return self._generate(inputs, max_length, stop_token_ids, strip_prompt)
    
    def _generate(
        self,
        inputs,
        max_length=None,
        stop_token_ids=None,
        strip_prompt=False,
        tokens_key = "token_ids",
        padding_mask_key = "padding_mask",        
    ):
        """Generate text tokens using the model.
        
        Args:
            inputs (dict): Input dictionary containing token IDs and padding 
                mask information. Must include keys specified by tokens_key 
                and padding_mask_key parameters.
            max_length (int, optional): Maximum total sequence length 
                (prompt + generated tokens). If None, generates until stop_token_ids 
                or maximum model sequence length is reached.
            stop_token_ids (List[int], optional): List of token IDs that stop 
                generation. Defaults to None.
            strip_prompt (bool, optional): If True, returns only the generated 
                tokens without the input prompt. If False, returns the full sequence 
                including the prompt. Defaults to False.
            tokens_key (str, optional): Key in the inputs dictionary for token IDs. 
                Defaults to "token_ids".
            padding_mask_key (str, optional): Key in the inputs dictionary for padding 
                mask. Defaults to "padding_mask".

        Returns:
            dict: Dictionary containing:
                - 'token_ids': Generated token IDs (numpy.ndarray)
                - 'padding_mask': Attention mask for the generated sequence (numpy.ndarray)
        
        Example: 
            ```
            preprocessor = PretrainingPreprocessor(
                tokenizer_handle="hf://google/gemma-2-2b",
                seq_len=100,
                model_type="maxtext",
            )
            
            prompt= "what is your name?"
            input = preprocessor.prepare_inference_input(prompt)

            pred_ids = model.generate(input, max_length=100)
            print(pred_ids)
            pred_text = preprocessor.tokenizer.decode(pred_ids["token_ids"][0])
            print(pred_text)
            ```
            
        """
        print("!!start generating ... ")
        if stop_token_ids is None:
            stop_token_ids = []
        print("!!stop_token_ids: ", stop_token_ids)
        jitted_generate_fn = self.make_generate_step()
        batch_size = inputs[tokens_key].shape[0]
        
        # Pad batch to be a multiple of fsdp dimension
        mesh = self.sharding_strategy.data_sharding.mesh
        devices_in_data_fsdp = mesh.shape[Axis.FSDP] if Axis.FSDP in mesh.shape else mesh.shape["fsdp"]
        remainder = batch_size % devices_in_data_fsdp
        if remainder != 0:
            pad_size = devices_in_data_fsdp - remainder        
            for key in inputs.keys():
                inputs[key] = np.pad(
                    inputs[key],
                    ((0, pad_size), (0, 0)),
                    mode='constant',
                    constant_values=0
                )
    
        def next_token(current_inputs):
            start_time = time.time()
            current_inputs = jax.device_put(
                current_inputs, self.sharding_strategy.data_sharding
                )
            logits = jitted_generate_fn(
                [v.value for v in self.model.trainable_variables],
                [v.value for v in self.model.non_trainable_variables],
                current_inputs,
            )
            jax.block_until_ready(logits)
            print(f"next_token time: {time.time() - start_time}")
            return logits

        tokens = inputs[tokens_key]
        segment_ids = inputs[padding_mask_key]

        # Calculate initial number of tokens (where segment_ids == 1)
        num_tokens = int(np.sum(segment_ids[0] == 1))
        seq_len = segment_ids.shape[1]

        # Calculate how many tokens we can/should generate
        max_length = min(seq_len, max_length) if max_length else seq_len
        generate_steps = max_length - num_tokens

        # Track which sequences have reached EOS
        reached_eos = [False for _ in range(batch_size)]

        for i in range(generate_steps):
            print(f"generating {i}th token ...")
            current_inputs = {
                **inputs,
                tokens_key: tokens,
                padding_mask_key: segment_ids,
            }

            # Get next token predictions
            logits = next_token(current_inputs)
            
            start_time = time.time()
            next_token_logits = logits[:, num_tokens - 1, :]
            next_tokens = keras.ops.argmax(next_token_logits, axis=-1)
            next_tokens = multihost_utils.process_allgather(next_tokens)

            # Update the tokens array with predictions
            tokens[:, num_tokens] = next_tokens

            # Update attention mask (segment_ids)
            segment_ids = np.roll(segment_ids, 1, axis=1)
            segment_ids[:, 0] = 1

            # Increment number of tokens
            num_tokens += 1

            # Check for EOS tokens
            for i, token in enumerate(next_tokens[:batch_size]):
                if token in stop_token_ids:
                    reached_eos[i] = True
            print(f"Postprocessing token time: {time.time() - start_time}")
            if all(reached_eos):
                break
        
        token_ids = tokens[:batch_size, :num_tokens]
        padding_mask = segment_ids[:batch_size, :num_tokens]
        
        if strip_prompt:
            token_ids = tokens[:batch_size, num_tokens - generate_steps:num_tokens]
            padding_mask = tokens[:batch_size, num_tokens - generate_steps:num_tokens]
        
        return {
            "padding_mask": padding_mask,
            "token_ids": token_ids,
        }

def set_precision(
    precision: Optional[str] = None,
    weight_dtype: Optional[str] = None,
    activation_dtype: Optional[str] = None,
) -> None:
    """
    Sets the precision policy for mixed precision training. This function overrides the
    default precision policy and must be called before loading the model. Note you do
    not need to manually call this function unless you are defining a custom model.

    Args:
        precision (Optional[str]): The precision policy to set. Can be one of
            'float32', 'float16', 'mixed_float16', or 'mixed_bfloat16'. If None, the
            precision will be inferred from `weight_dtype` and `activation_dtype`.
        weight_dtype (Optional[str]): The data type for weights. Used to infer
            the precision policy if `precision` is None. Must be one of
            'float32', 'float16', or 'bfloat16'.
        activation_dtype (Optional[str]): The data type for activations. Used to
            infer the precision policy if `precision` is None. Must be one of
            'float32', 'float16', or 'bfloat16'.

    Returns:
        precision (str): The precision policy that was set.
    """

    assert (
        precision is None
        and (weight_dtype is not None)
        and (activation_dtype is not None)
    ) or (
        (precision is not None)
        and (weight_dtype is None)
        and (activation_dtype is None)
    ), "Please only specify either weight and activation dtype, or precision, but not both."

    if precision is None:
        if weight_dtype == activation_dtype:
            precision = weight_dtype
        elif weight_dtype == "float32" and activation_dtype == "float16":
            precision = "mixed_float16"
        elif weight_dtype == "float32" and activation_dtype == "bfloat16":
            precision = "mixed_bfloat16"
        else:
            raise ValueError(
                "Weight dtype and activation dtype combination is not valid."
            )

    policy = global_state.get_global_attribute("dtype_policy", None)
    if policy:
        print(f"Overriding existing policy: {policy}")
    keras.mixed_precision.set_global_policy(precision)
    return precision


def set_global_sharding_strategy(strategy: Optional[ShardingStrategy]) -> None:
    """
    Sets the sharding strategy for the model and batch input.This function
    overrides the existing sharding strategy and must be called before loading
    the model.

    Args:
        strategy (Optional[kithara.ShardingStrategy]): The sharding strategy to be set
            globally. If None, no changes are made to the global state.
    """
    if strategy:
        if global_state.get_global_attribute("distribution") is not None:
            print("WARNING: Distribution strategy is being overridden.")
        set_distribution(strategy.distribution)
        global_state.set_global_attribute("DATA_SHARDING", strategy.data_sharding)
