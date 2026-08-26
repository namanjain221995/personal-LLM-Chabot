"""Model geometry, KV arithmetic, and the YaRN override argument.

The numbers asserted here are the ones measured on the DGX Spark cluster on
2026-08-25 (32,768 B/token at TP=1, 16,384 at TP=2, a 16 GiB budget holding
1,048,576 tokens), so a change to the formula that contradicts the running
deployment fails here rather than 30 minutes into a start-up.
"""

from __future__ import annotations

import json
import shlex
import tempfile
import unittest
from pathlib import Path

from techsara_cli.errors import TechSaraError
from techsara_cli.modelshape import (
    ModelShape,
    kv_bytes_per_token,
    kv_gib_for_tokens,
    kv_pool_tokens,
    read_model_shape,
    rope_override_argument,
    yarn_factor,
)

#: RadixArk/Qwen3.8-27B-NVFP4 as it really is: everything under text_config,
#: 48 gated-delta-net layers that hold no paged cache and 16 that do.
QWEN_ROPE = {
    "mrope_interleaved": True,
    "mrope_section": [11, 11, 10],
    "partial_rotary_factor": 0.25,
    "rope_theta": 10000000,
    "rope_type": "default",
}
NESTED_CONFIG = {
    "architectures": ["Qwen3NextForConditionalGeneration"],
    "text_config": {
        "max_position_embeddings": 262144,
        "num_hidden_layers": 64,
        "layer_types": ["linear_attention"] * 48 + ["full_attention"] * 16,
        "num_attention_heads": 24,
        "num_key_value_heads": 4,
        "head_dim": 256,
        "rope_parameters": dict(QWEN_ROPE),
        "rope_scaling": None,
    },
}
#: A plain transformer: no nesting, no layer_types, rope under rope_scaling.
FLAT_CONFIG = {
    "max_position_embeddings": 32768,
    "num_hidden_layers": 32,
    "num_attention_heads": 32,
    "num_key_value_heads": 8,
    "hidden_size": 4096,
    "rope_scaling": {"rope_type": "default", "rope_theta": 1000000},
}


def qwen_shape() -> ModelShape:
    shape = read_model_shape(config=NESTED_CONFIG)
    assert shape is not None
    return shape


class ReadModelShapeTests(unittest.TestCase):
    def test_a_nested_config_is_read_from_text_config(self) -> None:
        shape = qwen_shape()
        self.assertEqual(shape.native_context, 262144)
        # Only the full_attention layers page a KV cache.
        self.assertEqual(shape.full_attention_layers, 16)
        self.assertEqual(shape.num_key_value_heads, 4)
        self.assertEqual(shape.head_dim, 256)
        self.assertEqual(shape.rope_parameters, QWEN_ROPE)
        self.assertTrue(shape.nested)

    def test_a_flat_config_falls_back_to_layer_count_and_rope_scaling(self) -> None:
        shape = read_model_shape(config=FLAT_CONFIG)
        assert shape is not None
        self.assertEqual(shape.native_context, 32768)
        self.assertEqual(shape.full_attention_layers, 32)
        self.assertEqual(shape.num_key_value_heads, 8)
        self.assertEqual(shape.head_dim, 4096 // 32)
        self.assertEqual(shape.rope_parameters, {"rope_type": "default", "rope_theta": 1000000})
        self.assertFalse(shape.nested)

    def test_both_layouts_are_read_from_a_real_config_json(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            for name, document in (("nested", NESTED_CONFIG), ("flat", FLAT_CONFIG)):
                with self.subTest(layout=name):
                    directory = root / name
                    directory.mkdir()
                    (directory / "config.json").write_text(json.dumps(document), encoding="utf-8")
                    self.assertEqual(read_model_shape(directory), read_model_shape(config=document))

    def test_anything_unreadable_or_unfamiliar_returns_none_instead_of_raising(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self.assertIsNone(read_model_shape(root / "not-downloaded-yet"))
            (root / "config.json").write_text("{not json", encoding="utf-8")
            self.assertIsNone(read_model_shape(root))
        self.assertIsNone(read_model_shape(None))
        self.assertIsNone(read_model_shape(config={}))
        self.assertIsNone(read_model_shape(config=[1, 2, 3]))
        # A window but no usable geometry is still not something to guess at.
        self.assertIsNone(read_model_shape(config={"max_position_embeddings": 4096}))
        self.assertIsNone(
            read_model_shape(
                config={"max_position_embeddings": 4096, "num_hidden_layers": 8, "num_attention_heads": 0}
            )
        )
        # A non-positive window is as unusable as a missing one.
        self.assertIsNone(read_model_shape(config={**FLAT_CONFIG, "max_position_embeddings": 0}))

    def test_an_unfamiliar_layer_type_spelling_counts_every_layer(self) -> None:
        shape = read_model_shape(
            config={
                "text_config": {
                    **NESTED_CONFIG["text_config"],
                    "layer_types": ["mamba"] * 64,
                }
            }
        )
        assert shape is not None
        self.assertEqual(shape.full_attention_layers, 64, "never under-count the paged layers")


class KvArithmeticTests(unittest.TestCase):
    def test_bytes_per_token_matches_the_measured_cluster(self) -> None:
        shape = qwen_shape()
        self.assertEqual(kv_bytes_per_token(shape, tensor_parallel_size=1, kv_cache_dtype="fp8"), 32768)
        self.assertEqual(kv_bytes_per_token(shape, tensor_parallel_size=2, kv_cache_dtype="fp8"), 16384)
        for dtype in ("fp8_e4m3", "FP8", " fp8 "):
            with self.subTest(dtype=dtype):
                self.assertEqual(kv_bytes_per_token(shape, tensor_parallel_size=2, kv_cache_dtype=dtype), 16384)
        # Anything that is not an 8-bit cache stores two bytes per element.
        self.assertEqual(kv_bytes_per_token(shape, tensor_parallel_size=1, kv_cache_dtype="auto"), 65536)
        self.assertEqual(kv_bytes_per_token(shape, tensor_parallel_size=2, kv_cache_dtype="bfloat16"), 32768)
        # The default is one node with an fp8 cache.
        self.assertEqual(kv_bytes_per_token(shape), 32768)

    def test_the_pool_and_the_budget_are_inverses_of_each_other(self) -> None:
        # 16 GiB / 16,384 B is the live cluster's 1,048,576-token pool.
        self.assertEqual(kv_pool_tokens(16, 16384), 1048576)
        self.assertEqual(kv_pool_tokens(16, 32768), 524288)
        self.assertEqual(kv_gib_for_tokens(838860, 16384), 13)
        self.assertEqual(kv_gib_for_tokens(1048576, 16384), 16)
        # Rounded up: a budget that is one token short is not a budget.
        self.assertEqual(kv_gib_for_tokens(1048577, 16384), 17)
        self.assertEqual(kv_pool_tokens(16, 0), 0)
        self.assertEqual(kv_gib_for_tokens(1048576, 0), 0)


class YarnFactorTests(unittest.TestCase):
    def test_the_factor_is_rounded_up_to_two_decimals(self) -> None:
        # 800,000 / 262,144 = 3.0517...; rounding down would serve less than
        # the user asked for.
        self.assertEqual(yarn_factor(800000, 262144), 3.06)
        self.assertEqual(yarn_factor(838860, 262144), 3.2)
        self.assertEqual(yarn_factor(524288, 262144), 2.0)
        self.assertEqual(yarn_factor(262145, 262144), 1.01)

    def test_a_window_inside_the_native_one_never_scales(self) -> None:
        for requested in (4096, 131072, 262144):
            with self.subTest(requested=requested):
                self.assertEqual(yarn_factor(requested, 262144), 1.0)

    def test_beyond_four_times_native_is_refused_with_both_numbers(self) -> None:
        with self.assertRaises(TechSaraError) as caught:
            yarn_factor(1048577, 262144)
        message = str(caught.exception)
        self.assertIn("262,144", message)
        self.assertIn("1,048,576", message)
        self.assertIn("ceiling", message)
        # Exactly 4x is still supported.
        self.assertEqual(yarn_factor(1048576, 262144), 4.0)
        with self.assertRaisesRegex(TechSaraError, "native context length is unknown"):
            yarn_factor(800000, 0)


class RopeOverrideArgumentTests(unittest.TestCase):
    def test_the_argument_is_exact_compact_and_nested_under_text_config(self) -> None:
        rendered = rope_override_argument(qwen_shape(), 3.2)
        self.assertEqual(
            rendered,
            "--hf-overrides '{\"text_config\":{\"rope_parameters\":{"
            '"mrope_interleaved":true,"mrope_section":[11,11,10],"partial_rotary_factor":0.25,'
            '"rope_theta":10000000,"rope_type":"yarn","factor":3.2,'
            '"original_max_position_embeddings":262144}}}\'',
        )

    def test_every_existing_rope_key_survives_the_override(self) -> None:
        rendered = rope_override_argument(qwen_shape(), 3.06)
        payload = json.loads(shlex.split(rendered)[1])
        parameters = payload["text_config"]["rope_parameters"]
        for key, value in QWEN_ROPE.items():
            if key == "rope_type":
                continue
            self.assertEqual(parameters[key], value, f"{key} was dropped")
        self.assertEqual(parameters["rope_type"], "yarn")
        self.assertEqual(parameters["factor"], 3.06)
        self.assertEqual(parameters["original_max_position_embeddings"], 262144)

    def test_the_json_survives_shell_splitting_as_one_argv_element(self) -> None:
        argv = shlex.split(rope_override_argument(qwen_shape(), 3.2))
        self.assertEqual(len(argv), 2)
        self.assertEqual(argv[0], "--hf-overrides")
        self.assertEqual(json.loads(argv[1])["text_config"]["rope_parameters"]["factor"], 3.2)

    def test_no_extension_means_no_argument_at_all(self) -> None:
        for factor in (1.0, 0.5):
            with self.subTest(factor=factor):
                self.assertEqual(rope_override_argument(qwen_shape(), factor), "")


if __name__ == "__main__":
    unittest.main()
