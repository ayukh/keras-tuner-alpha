import os

# Use Jax backend
os.environ["KERAS_BACKEND"] = "jax"

import jax
import keras
import numpy as np
from functools import partial
from scalax.sharding import MeshShardingHelper, PartitionSpec, FSDPShardingRule
from keras_tuner.trainer.preprocessing import DefaultDataPreparationStrategy


class FSDPTrainer:
    def __init__(
        self,
        model,
        train_dataset,
        optimizer,
        tokenizer,
        eval_dataset=None,
        steps=None,
        seq_len=1024,
        log_steps=0,
        input_field="text",
        preprocess_strategy=DefaultDataPreparationStrategy(),
    ):
        self.model = model
        self.tokenizer = tokenizer
        self.optimizer = optimizer

        self.train_dataset = train_dataset
        self.eval_dataset = eval_dataset
        self.seq_len = seq_len
        self.input_field = input_field

        self.log_steps = log_steps
        self.steps = steps
        self.step_count = 0

        self.loss_fn = keras.losses.SparseCategoricalCrossentropy(
            from_logits=True, ignore_class=self.tokenizer.pad_token_id
        )

        # Create a 1D mesh with fsdp axis
        self.mesh = MeshShardingHelper([-1], ["fsdp"])

        # Make jitted training step
        self.train_step = self.make_train_step()
        self.data_preparation_strategy = preprocess_strategy

    def prepare_batch_input(self, batch):
        return self.data_preparation_strategy.prepare_training_input(
            batch, self.tokenizer, self.seq_len, self.input_field
        )

    def _train_step(self, state, data):

        (
            trainable_variables,
            non_trainable_variables,
            optimizer_variables,
        ) = state
        x, y = data["x"], data["y"]
        (loss, non_trainable_variables), grads = self.grad_fn(
            trainable_variables, non_trainable_variables, x, y
        )
        trainable_variables, optimizer_variables = self.optimizer.stateless_apply(
            optimizer_variables, grads, trainable_variables
        )

        return (
            loss,
            (
                trainable_variables,
                non_trainable_variables,
                optimizer_variables,
            ),
        )

    def make_train_step(self):
        @partial(
            self.mesh.sjit,
            # Expect input data to be replicated
            in_shardings=(
                FSDPShardingRule(),
                None,
            ),
            # Replicate loss, shard the state
            out_shardings=(None, FSDPShardingRule()),
            # Shard the data after the beginning of the function
            args_sharding_constraint=(
                FSDPShardingRule(),
                PartitionSpec("fsdp"),
            ),
            donate_argnums=(0,),
        )
        def compiled_train_step(state, data):
            return self._train_step(state, data)

        return compiled_train_step

    def train(self):
        self.optimizer.build(self.model.trainable_variables)

        trainable_variables = self.model.trainable_variables
        non_trainable_variables = self.model.non_trainable_variables
        optimizer_variables = self.optimizer.variables
        state = (
            trainable_variables,
            non_trainable_variables,
            optimizer_variables,
        )

        # Training loop
        while self.step_count < self.steps:
            for data in self.train_dataset:
                # Do data preprocessing
                data = self.prepare_batch_input(data)
                # Train for one step
                loss, state = self.train_step(state, data)
                self.step_count += 1
                if self.step_count % self.log_steps == 0:
                    print(f"Training loss at step {self.step_count}: {loss}")
                if self.step_count >= self.steps:
                    break

        self._update_model_with_state(state)

    def _update_model_with_state(self, state):
        """Update model internal parameters with the provided state"""
        trainable_variables, non_trainable_variables, *_ = state
        for variable, value in zip(self.model.trainable_variables, trainable_variables):
            variable.assign(value)
        for variable, value in zip(
            self.model.non_trainable_variables, non_trainable_variables
        ):
            variable.assign(value)

    def compute_loss(self, trainable_variables, non_trainable_variables, x, y):
        """This method is stateless and is intended for use with jax.grad."""
        logits, non_trainable_variables = self.model.stateless_call(
            trainable_variables, non_trainable_variables, x
        )
        loss = self.loss_fn(y, logits)

        return loss, non_trainable_variables

    @property
    def grad_fn(self):
        return jax.value_and_grad(self.compute_loss, has_aux=True)

    def _convert_text_to_model_input(self, prompt):
        """Convert input to model input for inference."""
        return self.data_preparation_strategy.prepare_inference_input(
            prompt, self.tokenizer, self.seq_len
        )

    def generate(self, prompt):
        """Generate response in inference mode."""
        input = self._convert_text_to_model_input(prompt)
        pred_ids = self.model.generate(
            input,
            stop_token_ids=[self.tokenizer.eos_token_id],
        )
        return self.tokenizer.decode(pred_ids["token_ids"][0])

    def save_model(self, filepath):
        """Save model weights in .h5 format"""
        self.model.save_weights(filepath)
