import torch

from . import META_COMPATIBILITY


def compatibility(is_backward_compatible: bool = False):
    """A decorator to make a function compatible with different versions of PyTorch.

    Args:
        is_backward_compatible (bool, optional): Whether the function is backward compatible. Defaults to False.

    Returns:
        Callable: The decorated function
    """

    def decorator(func):
        if META_COMPATIBILITY:
            return func
        else:
            if is_backward_compatible:
                return func
            else:

                def wrapper(*args, **kwargs):
                    raise RuntimeError(f'Function `{func.__name__}` is not compatible with PyTorch {torch.__version__}')

                return wrapper

    return decorator


def check_meta_compatibility():
    """Check the meta compatibility. Normally it should be called before importing some of the `colossalai.fx`
    modules. If the meta compatibility is not satisfied, the `colossalai.fx` modules will be replaced by its
    experimental counterparts.

    Returns:
        bool: The meta compatibility
    """
    return META_COMPATIBILITY
