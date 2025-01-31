# this file uses numpy / mlx in order to perform tensor operations

import numpy as np

try:
    import mlx.core as mx
    MLX_AVAILABLE = True
except ImportError as e:
    print(">>> Warning: MLX cannot be imported. Using numpy as default...")
    MLX_AVAILABLE = False

from typing import *
from functools import partial

from ..utils import broadcast_axis


# constants
DEFAULT_MIN = 1e-6
DEFAULT_MAX = 1 - 1e-6

# base type
Array = Union[np.ndarray, mx.array]


def _get_d(device: str = "gpu"):
    # this is a bit misleading because mlx has unified cpu/gpu ram
    # will be fixing this soon
    # also return numpy if mlx is not found or available
    if device == "cpu":    return np
    if device == "gpu":    return mx if MLX_AVAILABLE else np


class no_grad:
    """
    Context-manager that disables 
    gradient calculation.
    """

    def __enter__(self):
        self.previous = Tensor.grad_is_enabled
        Tensor.grad_is_enabled = False

    def __exit__(
            self, exc_type, 
            exc_value, traceback
        ):
        Tensor.grad_is_enabled = self.previous


class Tensor:
    """
    holds elements having the same dtype
    """
    grad_is_enabled: bool = True

    def __init__(
            self,
            data: Union[Array, Any],
            dtype = None,
            _children: tuple = (),
            _op = None,
            requires_grad: bool = False,
            use_np: bool = False
        ) -> None:
        
        self.is_np_tensor = use_np
        self._d = _get_d(device="gpu" if not use_np else "cpu")
        self.dtype = dtype or self._d.float32
        
        # actual data
        self.data = (
            self._d.array(data, self.dtype) if not isinstance(data, Array) 
            else data.astype(dtype=self.dtype)
        )

        # operation this tensor originates from along with children
        self._prev = set([c for c in _children if c.requires_grad])
        self._op = _op

        # gradient
        self.requires_grad = requires_grad
        self.grad = (
            self._d.zeros_like(self.data) if self.requires_grad and self.grad_is_enabled 
            else None
        )
        self.grad_fn = None

        self.shape = self.data.shape
        self.ndim = len(self.shape)

    def _reset_grad(self) -> None:
        """
        Sets the gradient values to zero
        """
        self.grad = self._d.zeros_like(self.data)

    def set_requires_grad(self, val: bool) -> None:
        if not isinstance(val, bool):
            raise ValueError("Value should be boolean")
        
        if self.grad is None and val == True:
            self._reset_grad()

        self.requires_grad = val

    def backward(self) -> None:
        """
        sort the graph topologically.
        run the grad function from the last node to first
        """
        if not self.grad_is_enabled:
            raise ValueError("cannot backward when gradient calculation is disabled.")
        
        ordering = []
        
        visited = set()
        recursion_stack = set()
        
        def _tsort(curr: "Tensor"):
            if curr in recursion_stack:
                raise ValueError("Graph contains a cycle")
            if curr not in visited:
                visited.add(curr)
                recursion_stack.add(curr)

                for child in curr._prev:
                    _tsort(child)
                
                recursion_stack.remove(curr)
                ordering.append(curr)

        _tsort(self)

        # gradient wrt to self is always 1
        self.grad = self._d.ones_like(self.data)

        # gradient on each previous node
        for node in reversed(ordering):
            if node.grad_fn is not None:
                node.grad_fn()
    
    def clip(
            self, min: float = DEFAULT_MIN, 
            max: float = DEFAULT_MAX, clip_grad: bool = False, 
            grad_min: float = DEFAULT_MIN, grad_max: float = DEFAULT_MAX
        ) -> "Tensor":
        """
        constrain the Tensor/gradient values between given min max range
        """

        self.data = self._d.clip(self.data, min, max)

        if clip_grad and self.grad is not None:
            self.grad = self._d.clip(self.grad, grad_min, grad_max)

        return self
    
    def __setitem__(self, indices, other):
        """
        Set values of the tensor using indices.
        """
        assert self._d == other._d, f"Tensors must be of the same type i.e. numpy or mlx"
        assert isinstance(other, Tensor)

        self.data[indices] = other.data.astype(self.data.dtype).copy()
        self.grad[indices] = other.grad.astype(self.grad.dtype).copy()

    def __getitem__(self, indices):
        """
        Get a subset of the tensor using indices.
        """

        out = Tensor(
            self.data[indices], dtype=self.dtype, 
            _children=(self, ), _op="getitem", 
            requires_grad=self.requires_grad, use_np=self.is_np_tensor
        )

        if self.requires_grad and self.grad_is_enabled:
            def _getitem_backward():
                self.grad[indices] += out.grad

            out._backward = _getitem_backward
            out.requires_grad = True

        return out

    # ----------------------- UNARY OPS --------------------------------

    def sum(self, axis: Union[int, Tuple[int]] = None, keepdims: bool = False) -> "Tensor":
        """
        sum values of tensor along given axes
        """
        out = Tensor(
            self._d.sum(self.data, axis=axis, keepdims=keepdims),
            _children=(self, ), _op="sum", use_np=self.is_np_tensor
        )

        if self.requires_grad and self.grad_is_enabled:
            ex_axis = axis if axis and not keepdims else None

            def _sum_backward():
                if ex_axis:
                    self.grad += self._d.ones_like(self.grad) * self._d.expand_dims(
                        out.grad, axis=ex_axis
                    )
                else:
                    self.grad += self._d.ones_like(self.grad) * out.grad

            out.grad_fn = _sum_backward
            out.set_requires_grad(True)

        return out
    
    def mean(self, axis: int = None, keepdims: bool = False) -> "Tensor":
        """
        calculate the arithmetic average of the Tensor elements along given axis
        """
        N = self.data.size
        if isinstance(axis, int):
            N = self.data.shape[axis]
        if isinstance(axis, (tuple, list)):
            N = 1
            for dim in axis:
                N *= dim
        
        # backward gradient flow already defined
        out: Tensor = self.sum(axis=axis, keepdims=keepdims) / N
        return out
    
    def _stdvar_helper__(self, axis: int = None, keepdims: bool = False, correction: int = 0) -> "Tensor":
        N: int = self.data.shape[axis] if axis is not None else self.data.size
        assert N - correction > 0, "Correction should not be greater than or equal to number of samples."

        # composed operations i.e. no need to write backward function
        t = (self - self.mean(axis=axis, keepdims=True)) ** 2
        t1 = t.sum(axis=axis, keepdims=keepdims) / (N - correction)
        
        return t1
    
    def std(self, axis: int = None, keepdims: bool = False, correction: int = 0) -> "Tensor":
        """
        calculate the standard deviation of the Tensor elements along given axis
        """
        t1 = self._stdvar_helper__(axis=axis, keepdims=keepdims, correction=correction)
        return t1 ** (1/2)
    
    def var(self, axis: int = None, keepdims: bool = False, correction: int = 0) -> "Tensor":
        """
        calculate variance along given axis and correction
        """
        return self._stdvar_helper__(axis=axis, keepdims=keepdims, correction=correction)
    
    def half(self) -> "Tensor":
        """
        convert the data and gradients to half precision i.e. float32 -> float16
        """
        if self.dtype == self._d.float32:
            out = Tensor(
                self.data, dtype=self._d.float16, 
                _children = (self, ), _op = "half",
                use_np=self.is_np_tensor
            )

            if self.requires_grad and self.grad_is_enabled:
                # just copy the gradients backward
                def _half_backward():
                    self.grad += out.grad
                
                out.grad_fn = _half_backward
                out.set_requires_grad(True)

            return out
        
        else:
            raise ValueError(f"Cannot convert Tensor with dtype {self.dtype} to half precision.")

    def T(self, axes: Iterable = None) -> "Tensor":
        """
        transposes a given tensor along the given axes
        """

        out = Tensor(
            self._d.transpose(self.data, axes=axes),
            _children=(self, ), _op='T', use_np=self.is_np_tensor
        )

        if self.requires_grad and self.grad_is_enabled:
            def _transpose_backward():
                self.grad += self._d.transpose(out.grad, axes=axes)
            
            out.grad_fn = _transpose_backward
            out.set_requires_grad(True)
        
        return out
    
    def exp(self) -> "Tensor":
        """
        elementwise e to the power data
        """

        out = Tensor(self._d.exp(self.data), _children=(self, ), _op='exp', use_np=self.is_np_tensor)
        
        # since d/dx (exp(x)) = exp(x) = out.data
        if self.requires_grad and self.grad_is_enabled:
            def _exp_backward():
                self.grad += out.data * out.grad

            out.grad_fn = _exp_backward
            out.set_requires_grad(True)
        
        return out

    def log(self) -> "Tensor":
        """
        log base e of the tensor
        """
        
        out = Tensor(self._d.log(self.data), _children=(self, ), _op="log", use_np=self.is_np_tensor)

        # since d/dx (log x) = 1 / x
        if self.requires_grad and self.grad_is_enabled:
            def _log_backward():
                self.grad += (out.grad / self.data)

            out.grad_fn = _log_backward
            out.set_requires_grad(True)

        return out
    
    def reshape(self, *shape: Tuple[int]) -> "Tensor":
        """
        change the tensor's shape
        """
        out = Tensor(
            self.data.reshape(shape), dtype=self.dtype, use_np=self.is_np_tensor,
            _children=(self, ), _op="reshape"
        )

        if self.requires_grad and self.grad_is_enabled:
            def _reshape_backward():
                self.grad += out.grad.reshape(self.data.shape)

            out.grad_fn = _reshape_backward
            out.set_requires_grad(True)

        return out

    def masked_fill(self, mask: Union[Array, list], value: Any) -> "Tensor":
        assert isinstance(mask, Array) or isinstance(mask, list)
        if isinstance(mask, list):
            mask = self._d.array(mask, dtype=self._d.int8)

        data = self._d.where(mask, self._d.array(value), self.data)
        out = Tensor(
            data, dtype=self.dtype, _children=(self, ), _op="mafill", 
            requires_grad=self.requires_grad, use_np=self.is_np_tensor
        )

        if self.requires_grad and self.grad_is_enabled:
            def _masked_fill_backward():
                self.grad += self._d.where(mask, self._d.array(0), out.grad)
        
            out.grad_fn = _masked_fill_backward
            out.set_requires_grad(True)
        
        return out

    # ------------------------ BINARY OPS -------------------------

    def cat(self, others: List["Tensor"], dim: Optional[int] = 0) -> "Tensor":
        """
        concatenate self and other along a given dimension
        """
        tocat: List[Tensor] = [self]
        for _o in others:
            assert isinstance(_o, Tensor), f"Cannot concatenate type '{type(_o)}'"
            assert self._d == _o._d, f"Tensors must be of the same type i.e. numpy or mlx"
            tocat.append(_o)

        out = Tensor(
            self._d.concatenate([t.data for t in tocat], axis=dim),
            _children=tuple(tocat), _op="cat", use_np=self.is_np_tensor
        )
        _allfalse = self._d.array([not ob.requires_grad for ob in tocat])

        # no children require gradients
        if self._d.all(_allfalse).item():
            return out
        
        if self.grad_is_enabled:
            sizes = [t.shape[dim] for t in tocat]
            sizes = self._d.array(sizes[:-1])
            splits = self._d.cumsum(sizes).tolist()
            grads = self._d.split(out.grad, splits, axis=dim)

            def _cat_backward():
                for i, tsor in enumerate(tocat):
                    if tsor.requires_grad:
                        tsor.grad += grads[i]

            out.grad_fn = _cat_backward
            out.set_requires_grad(True)

        return out
    
    def split(self, sections: int, dim: int = 0) -> List["Tensor"]:
        """
        splits the tensor into equal sections along the given dimension
        """
        datas: List[Array] = self._d.split(self.data, sections, axis=dim)
        outs: List[Tensor] = [
            Tensor(
                _d, self.dtype, _children=(self, ), _op="split", 
                requires_grad=self.requires_grad, use_np=self.is_np_tensor
            ) for _d in datas
        ]

        if not self.requires_grad:  return outs
        if self.grad_is_enabled:
            # update corresponding subarray of gradients using indices
            indices, start = [], 0
            for part in outs:
                idx = [slice(None)] * self.ndim
                idx[dim] = slice(start, start + part.shape[dim])
                indices.append(tuple(idx))
                start += part.shape[dim]
            
            def _split_backward(index: int = 0):
                _o, idx = outs[index], indices[index]
                if self.requires_grad:
                    self.grad[idx] += _o.grad

            # set the backward for different split parts
            for i, part in enumerate(outs):
                _grad_fn = partial(_split_backward, index = i)
                part.grad_fn = _grad_fn
                part.set_requires_grad(True)
        return outs

    def __matmul__(self, other) -> "Tensor":
        """
        matrix multiplication with tensors
        """

        assert self._d == other._d, f"Tensors must be of the same type i.e. numpy or mlx"

        other = other if isinstance(other, Tensor) else Tensor(other, use_np=self.is_np_tensor)
        
        out = Tensor(self.data @ other.data, _children=(self, other), _op="@", use_np=self.is_np_tensor)
        if not self.requires_grad and not other.requires_grad:
            return out

        if self.grad_is_enabled:
            # for 1D tensors, expand first dimension for first tensor
            # and expand last dimension for second tensor
            # example: (3,) @ (3,) becomes (1, 3) and (3, 1)
            # which is compatible for matrix multiplication
            le_axis = (0, ) if self.data.ndim == 1 else ()
            re_axis = (-1, ) if other.data.ndim == 1 else ()

            # resultant tensor's grad should be expanded by both le_axis and re_axis
            rese_axis = le_axis + re_axis

            # we need to take broadcasting into account
            # except last two dimensions of shape (since they will be used for matmul)
            # gradients will be summed along the broadcasted axes for both tensors
            l, r = broadcast_axis(self.data.shape[:-2], other.data.shape[:-2])

            # for 2D (can be generalized for more dimensions too):
            #
            # self.grad = out.grad @ other.data.T
            # other.grad = self.data.T @ out.grad

            def _matmul_backward():
                if self.requires_grad:
                    self.grad = self._d.reshape(
                        self._d.sum(
                            self._d.expand_dims(out.grad, axis=rese_axis) @
                            self._d.expand_dims(other.data, axis=re_axis).swapaxes(-1, -2),
                            axis = l
                        ),
                        self.data.shape
                    )
                if other.requires_grad:
                    other.grad = self._d.reshape(
                        self._d.sum(
                            self._d.expand_dims(self.data, axis=le_axis).swapaxes(-1, -2) @
                            self._d.expand_dims(out.grad, axis=rese_axis),
                            axis = r
                        ),
                        other.data.shape
                    )

            out.grad_fn = _matmul_backward
            out.set_requires_grad(True)

        return out

    def __add__(self, other) -> "Tensor":
        """
        elementwise add (takes broadcasting into account)
        """

        if isinstance(other, (int, float)):
            out = Tensor(self.data + other, _children=(self, ), _op='+', use_np=self.is_np_tensor)

            if self.requires_grad and self.grad_is_enabled:
                def _add_backward_scalar():
                        self.grad += out.grad

                out.grad_fn = _add_backward_scalar
                out.set_requires_grad(True)
            
            return out
                        
        else:
            other = other if isinstance(other, Tensor) else Tensor(other, use_np=self.is_np_tensor)
            assert self._d == other._d, f"Tensors must be of the same type i.e. numpy or mlx"
            
            out = Tensor(self.data + other.data, _children=(self, other), _op='+')

            if self.requires_grad == False and other.requires_grad == False:
                return out
            
            if self.grad_is_enabled:
                if self.shape == other.shape:
                    # same shape, so gradient for addition will be just propagated
                    # backwards equally to self and other from the resultant Tensor (out)
                    def _add_backward_same():
                        if self.requires_grad:
                            self.grad += out.grad
                        if other.requires_grad:
                            other.grad += out.grad   

                    out.grad_fn = _add_backward_same
                
                else:
                    # different shapes, broadcast occurs
                    # gradient will be summed along the broadcasted axes
                    # since the out Tensor is result of broadcasting and addition
                    # in essence, broadcasted axes are copied and added, so gradients from 
                    # all the copies should be added
                    laxis, raxis = broadcast_axis(self.data.shape, other.data.shape)

                    def _add_backward_diff():
                        if self.requires_grad:
                            self.grad += self._d.reshape(
                                self._d.sum(out.grad, axis=laxis), self.shape
                            )
                        if other.requires_grad:
                            other.grad += self._d.reshape(
                                self._d.sum(out.grad, axis=raxis), other.shape
                            )
                    
                    out.grad_fn = _add_backward_diff

            out.set_requires_grad(True)
            return out
    
    def __mul__(self, other) -> "Tensor":
        """
        element wise multiply (takes broadcasting into account)
        """

        if isinstance(other, (int, float)):
            out = Tensor(self.data * other, _children=(self, ), _op='*', use_np=self.is_np_tensor)

            if self.requires_grad and self.grad_is_enabled:
                def _mul_backward_scalar():
                    self.grad += other * out.grad

                out.grad_fn = _mul_backward_scalar
                out.set_requires_grad(True)
            
            return out
        
        else:
            other = other if isinstance(other, Tensor) else Tensor(other, use_np=self.is_np_tensor)
            assert self._d == other._d, f"Tensors must be of the same type i.e. numpy or mlx"

            out = Tensor(self.data * other.data, _children=(self, other), _op='*')
            
            if self.requires_grad == False and other.requires_grad == False:
                return out
            
            if self.grad_is_enabled:
                if self.shape == other.shape:
                    def _mul_backward_same():
                        if self.requires_grad:
                            self.grad += other.data * out.grad
                        if other.requires_grad:
                            other.grad += self.data * out.grad

                    out.grad_fn = _mul_backward_same

                else:
                    # for broadcast multiply
                    # different shapes, broadcast occurs
                    # gradient will be summed along the broadcasted axes
                    # since the out Tensor is result of broadcasting and addition
                    # in essence, broadcasted axes are copied and added, so gradients from 
                    # all the copies should be added
                    laxis, raxis = broadcast_axis(self.data.shape, other.data.shape)

                    def _mul_backward_diff():
                        if self.requires_grad:
                            self.grad += self._d.reshape(
                                self._d.sum(other.data * out.grad, axis=laxis), self.shape
                            )
                        if other.requires_grad:
                            other.grad += self._d.reshape(
                                self._d.sum(self.data * out.grad, axis=raxis), other.shape
                            )
                    
                    out.grad_fn = _mul_backward_diff

            out.set_requires_grad(True)
            return out
    
    def __pow__(self, other: Union[int, float]) -> "Tensor":
        """
        raise the tensor to some int or float power
        """

        assert isinstance(other, (int, float)), f"Tensor power for {type(other)} is not supported."

        # numpy and mlx don't allow raising by negative values
        def _neg_pow(a, b: Union[int, float]):
            assert isinstance(b, (int, float))
            return 1 / (a ** abs(b)) if b < 0 else a ** b
        
        out = Tensor(
            _neg_pow(self.data, other), _children=(self, ), _op="pow", use_np=self.is_np_tensor 
        )

        # gradient is: p * a^(p-1)
        if self.requires_grad and self.grad_is_enabled:
            def _pow_backward():
                self.grad += other * _neg_pow(self.data, other - 1) * out.grad

            out.grad_fn = _pow_backward
            out.set_requires_grad(True)

        return out
    
    def __neg__(self):
        return self * -1
    
    def __sub__(self, other: "Tensor"):
        return self + (-other)
    
    def __rsub__(self, other: "Tensor"):    # other - self
        return -self + other
    
    def __radd__(self, other: "Tensor"):  # other + self
        return self + other
    
    def __rmul__(self, other: "Tensor"):  # other * self
        return self * other

    def __truediv__(self, other: "Tensor"):
        return self * (other ** -1)
    
    def __rtruediv__(self, other: "Tensor"):    # other / self
        return (self ** -1) * other
    
    def __repr__(self) -> str:
        return f"Tensor({self.data}, is_mlx_tensor={not self.is_np_tensor})"
