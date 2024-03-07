#!/usr/bin/env python
from dataclasses import dataclass
from typing import Callable, List, Union

__all__ = ["ModelZooRegistry", "ModelAttribute", "model_zoo"]


@dataclass
class ModelAttribute:
    """
    Attributes of a model.

    Args:
        has_control_flow (bool): Whether the model contains branching in its forward method.
        has_stochastic_depth_prob (bool): Whether the model contains stochastic depth probability. Often seen in the torchvision models.
    """

    has_control_flow: bool = False
    has_stochastic_depth_prob: bool = False


class ModelZooRegistry(dict):
    """
    A registry to map model names to model and data generation functions.
    """

    def register(
        self,
        name: str,
        model_fn: Callable,
        data_gen_fn: Callable,
        output_transform_fn: Callable,
        loss_fn: Callable = None,
        model_attribute: ModelAttribute = None,
    ):
        """
        Register a model and data generation function.

        Examples:

        ```python
        # normal forward workflow
        model = resnet18()
        data = resnet18_data_gen()
        output = model(**data)
        transformed_output = output_transform_fn(output)
        loss = loss_fn(transformed_output)

        # Register
        model_zoo = ModelZooRegistry()
        model_zoo.register('resnet18', resnet18, resnet18_data_gen, output_transform_fn, loss_fn)
        ```

        Args:
            name (str): Name of the model.
            model_fn (Callable): A function that returns a model. **It must not contain any arguments.**
            data_gen_fn (Callable): A function that returns a data sample in the form of Dict. **It must not contain any arguments.**
            output_transform_fn (Callable): A function that transforms the output of the model into Dict.
            loss_fn (Callable): a function to compute the loss from the given output. Defaults to None
            model_attribute (ModelAttribute): Attributes of the model. Defaults to None.
        """
        self[name] = (model_fn, data_gen_fn, output_transform_fn, loss_fn, model_attribute)

    def get_sub_registry(
        self, keyword: Union[str, List[str]], exclude: Union[str, List[str]] = None, allow_empty: bool = False
    ):
        """
        Get a sub registry with models that contain the keyword.

        Args:
            keyword (str): Keyword to filter models.
        """
        new_dict = dict()

        if isinstance(keyword, str):
            keyword_list = [keyword]
        else:
            keyword_list = keyword
        assert isinstance(keyword_list, (list, tuple))

        if exclude is None:
            exclude_keywords = []
        elif isinstance(exclude, str):
            exclude_keywords = [exclude]
        else:
            exclude_keywords = exclude
        assert isinstance(exclude_keywords, (list, tuple))

        for k, v in self.items():
            for kw in keyword_list:
                if kw in k:
                    should_exclude = False
                    for ex_kw in exclude_keywords:
                        if ex_kw in k:
                            should_exclude = True

                    if not should_exclude:
                        new_dict[k] = v

        if not allow_empty:
            assert len(new_dict) > 0, f"No model found with keyword {keyword}"
        return new_dict


model_zoo = ModelZooRegistry()