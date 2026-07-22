"""Champion-model ONNX export.

The champion is a logistic model, so its ONNX graph is tiny:
``sigmoid(X @ W + b)`` -- a MatMul, an Add, and a Sigmoid. We build the graph
directly with the ``onnx`` helpers (no skl2onnx dependency). ``onnx`` is
imported lazily so the rest of the package works without it; call
:func:`onnx_available` to check.

In-memory inference does not require ONNX (the numpy backend is the default,
benchmarked low-overhead runtime). ONNX export exists for portability and for a
future Rust/ONNX-Runtime execution service.
"""

from __future__ import annotations

import numpy as np

from .residual_model import LinearModel


def onnx_available() -> bool:
    try:
        import onnx  # noqa: F401
        return True
    except Exception:
        return False


def onnxruntime_available() -> bool:
    try:
        import onnxruntime  # noqa: F401
        return True
    except Exception:
        return False


def build_onnx_model(model: LinearModel, *, input_name: str = "features", output_name: str = "win_prob"):
    """Build an in-memory ONNX ModelProto for the logistic champion."""

    import onnx
    from onnx import TensorProto, helper, numpy_helper

    n_features = model.weights.shape[0]
    w = model.weights.astype(np.float32).reshape(n_features, 1)
    b = np.array([model.bias], dtype=np.float32)

    w_init = numpy_helper.from_array(w, name="W")
    b_init = numpy_helper.from_array(b, name="b")

    x = helper.make_tensor_value_info(input_name, TensorProto.FLOAT, [None, n_features])
    y = helper.make_tensor_value_info(output_name, TensorProto.FLOAT, [None, 1])

    nodes = [
        helper.make_node("MatMul", [input_name, "W"], ["xw"]),
        helper.make_node("Add", ["xw", "b"], ["z"]),
        helper.make_node("Sigmoid", ["z"], [output_name]),
    ]
    graph = helper.make_graph(nodes, "residual_win_prob", [x], [y], initializer=[w_init, b_init])
    opset = helper.make_operatorsetid("", 13)
    onnx_model = helper.make_model(graph, opset_imports=[opset], producer_name="moneymaker")
    onnx.checker.check_model(onnx_model)
    return onnx_model


def export_to_onnx(model: LinearModel, path: str) -> str:
    """Serialize the champion to an .onnx file. Requires the ``onnx`` package."""

    import onnx

    onnx_model = build_onnx_model(model)
    onnx.save(onnx_model, path)
    return path
