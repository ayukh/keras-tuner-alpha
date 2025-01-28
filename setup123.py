from setuptools import setup, find_packages

setup(
    packages=find_packages(),
    dependencies = [
        "flax>=0.7.0",
        "datasets",
        "huggingface-hub",
        "jax>=0.4.30",
        "jaxlib>=0.4.30",
        "keras>=3.8.0",
        "transformers>=4.45.1",
        "keras-nlp>=0.18.1",
        "google-api-python-client",
        "google-auth-httplib2",
        "google-auth-oauthlib",
        "ray[default]",
        "torch",
        "peft",
        "hf_transfer",
        "tabulate",
        # MaxText dependencies
        "aqtp",
        "grain-nightly",
        "orbax-checkpoint>=0.10.3",
        "google-cloud-logging",
        "tensorboardx",
        "ml-collections",
        "tensorflow_datasets",
        "sentencepiece",
        "tiktoken",
        "pathwaysutils@git+https://github.com/google/pathways-utils.git",
        "cloud-accelerator-diagnostics",
        "cloud-tpu-diagnostics",
        "ml-goodput-measurement"
    ]
)
