from ..core import Tensor
from ._module import Module


def _check_tensor_types(a: Tensor, b: Tensor):
    if a._d != b._d:
        raise RuntimeError("Expected both Tensors to be either mlx or np.")


class MSELoss(Module):
    """
    Creates a criterion that measures the mean squared error (squared L2 norm) 
    between each element in pred and target of size n based on reduction.

    If reduction is "sum", it doesn't divide by n to get mean
    If reduction is "mean", it divides by n to get mean
    """
    def __init__(self, reduction: str = "sum") -> None:
        self.reduction = reduction

    def forward(self, pred: Tensor, actual: Tensor) -> Tensor:
        _check_tensor_types(pred, actual)

        l2sum = ((pred - actual) ** 2).sum()
        
        if self.reduction == "sum":     return l2sum
        elif self.reduction == "mean":  return l2sum / actual.shape[0]

        else:   raise ValueError(f"Invalid reduction type '{self.reduction}' found.")
