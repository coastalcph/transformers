# coding=utf-8
# Copyright 2023 The HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Convert a RKWV checkpoint form BlinkDL to the Hugging Face format."""


import argparse
import gc
import json
import os
import re

import torch
from huggingface_hub import hf_hub_download

from transformers import PreTrainedTokenizerFast, RwkvConfig
from transformers.modeling_utils import WEIGHTS_INDEX_NAME, shard_checkpoint


NUM_HIDDEN_LAYERS_MAPPING = {
    "169M": 12,
    "430M": 24,
    "1B5": 24,
    "3B": 32,
    "7B": 32,
    "14B": 40,
}

HIDEN_SIZE_MAPPING = {
    "169M": 768,
    "430M": 1024,
    "1B5": 2048,
    "3B": 2560,
    "7B": 4096,
    "14B": 5120,
}

def convert_state_dict(state_dict):
    state_dict_keys = list(state_dict.keys())
    for name in state_dict_keys:
        weight = state_dict.pop(name)
        # emb -> embedding
        if name.startswith("emb."):
            name = name.replace("emb.", "embeddings.")
        # ln_0 -> pre_ln (only present at block 0)
        if name.startswith("blocks.0.ln0"):
            name = name.replace("blocks.0.ln0", "blocks.0.pre_ln")
        # att -> attention
        name = re.sub(r"blocks\.(\d+)\.att", r"blocks.\1.attention", name)
        # ffn -> feed_forward
        name = re.sub(r"blocks\.(\d+)\.ffn", r"blocks.\1.feed_forward", name)
        # time_mix_k -> time_mix_key and reshape
        if name.endswith(".time_mix_k"):
            name = name.replace(".time_mix_k", ".time_mix_key")
        # time_mix_v -> time_mix_value and reshape
        if name.endswith(".time_mix_v"):
            name = name.replace(".time_mix_v", ".time_mix_value")
        # time_mix_r -> time_mix_key and reshape
        if name.endswith(".time_mix_r"):
            name = name.replace(".time_mix_r", ".time_mix_receptance")

        if name != "head.weight":
            name = "rwkv." + name

        state_dict[name] = weight
    return state_dict


def convert_rmkv_checkpoint_to_hf_format(repo_id, checkpoint_file, model_file, output_dir, size=None, tokenizer_file=None):
    # 1. If possible, build the tokenizer.
    if tokenizer_file is None:
        print("No `--tokenizer_file` provided, we will only convert the model.")
        vocab_size = 50277
    else:
        tokenizer = PreTrainedTokenizerFast(tokenizer_file=tokenizer_file)
        vocab_size = len(tokenizer)
        tokenizer.save_pretrained(output_dir)

    # 2. Build the config
    possible_sizes = list(NUM_HIDDEN_LAYERS_MAPPING.keys())
    if size is not None:
        if type(size) is tuple:
            num_hidden_layers, hidden_size = size
        elif size not in possible_sizes:
            raise ValueError(f"`size` should be one of {possible_sizes}, got {size}.")
    else:
        # Try to infer size from the checkpoint name
        for candidate in possible_sizes:
            if candidate in checkpoint_file:
                size = candidate
                break
        if size is None:
            raise ValueError("Could not infer the size, please provide it with the `--size` argument.")
        num_hidden_layers = HIDEN_SIZE_MAPPING[size]
        hidden_size = HIDEN_SIZE_MAPPING[size]
    config = RwkvConfig(
        vocab_size=vocab_size,
        num_hidden_layers=num_hidden_layers,
        hidden_size=hidden_size,
    )
    config.save_pretrained(output_dir)

    # 3. Download model file then convert state_dict
    if repo_id is not None and checkpoint_file is not None:
        model_file = hf_hub_download(repo_id, checkpoint_file)
    else:
        model_file = model_file
    state_dict = torch.load(model_file, map_location="cpu")
    state_dict = convert_state_dict(state_dict)

    # 4. Split in shards and save
    shards, index = shard_checkpoint(state_dict)
    for shard_file, shard in shards.items():
        torch.save(shard, os.path.join(output_dir, shard_file))

    if index is not None:
        save_index_file = os.path.join(output_dir, WEIGHTS_INDEX_NAME)
        # Save the index as well
        with open(save_index_file, "w", encoding="utf-8") as f:
            content = json.dumps(index, indent=2, sort_keys=True) + "\n"
            f.write(content)

        # 5. Clean up shards (for some reason the file PyTorch saves take the same space as the whole state_dict
        print(
            "Cleaning up shards. This may error with an OOM error, it this is the case don't worry you still have converted the model."
        )
        shard_files = list(shards.keys())

        del state_dict
        del shards
        gc.collect()

        for shard_file in shard_files:
            state_dict = torch.load(shard_file)
            torch.save({k: v.cpu().clone() for k, v in state_dict.items()}, shard_file)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    # Required parameters
    parser.add_argument(
        "--repo_id", default=None, type=str, required=False, help="Repo ID from which to pull the checkpoint."
    )
    parser.add_argument(
        "--checkpoint_file", default=None, type=str, required=False, help="Name of the checkpoint file in the repo."
    )
    parser.add_argument(
        "--model_file", default=None, type=str, required=False)
    parser.add_argument(
        "--output_dir", default=None, type=str, required=True, help="Where to save the converted model."
    )
    parser.add_argument(
        "--tokenizer_file",
        default=None,
        type=str,
        help="Path to the tokenizer file to use (if not provided, only the model is converted).",
    )
    parser.add_argument(
        "--size",
        default=None,
        type=str,
        help="Size of the model. Will be inferred from the `checkpoint_file` if not passed.",
    )
    parser.add_argument(
        "--hidden_size",
        default=256,
        type=int
    )
    parser.add_argument(
        "--num_hidden_layers",
        default=12,
        type=int
    )

    args = parser.parse_args()
    if args.size is not None:
        size = args.size
    else:
        size = (args.num_hidden_layers, args.hidden_size)
    convert_rmkv_checkpoint_to_hf_format(
        args.repo_id, args.checkpoint_file, args.model_file, args.output_dir, size=size, tokenizer_file=args.tokenizer_file
    )
