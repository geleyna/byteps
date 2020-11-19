# Copyright 2019 Bytedance Inc. or its affiliates. All Rights Reserved.
# Copyright 2017 Uber Technologies, Inc. All Rights Reserved.
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
# ==============================================================================

import byteps.tensorflow as bps
import tensorflow as tf
from byteps.tensorflow.ops import _push_pull_xla_v2, _sync_tensors_handle_out_v2
from byteps.tensorflow.ops import _my_barrier_handle_out
from byteps.tensorflow import push_pull_xla_handle_out_v2
import os

enable_xla = os.environ.get('BYTEPS_ENABLE_XLA', '0')

def create_distributed_optimizer(keras, optimizer, name, device_dense, device_sparse,
                                 compression, sparse_as_dense):
    class _DistributedOptimizer(keras.optimizers.Optimizer):
        _HAS_AGGREGATE_GRAD = True
        def __init__(self, **kwargs):
            self._name = name or "Distributed%s" % self.__class__.__base__.__name__
            self._device_dense = device_dense
            self._device_sparse = device_sparse
            self._compression = compression
            self._sparse_as_dense = sparse_as_dense
            self._aggregated_gradients = False

            if enable_xla == '1':
                self._push_pull = self._push_pull_xla
            else:
                self._push_pull = self._push_pull_tf

            super(self.__class__, self).__init__(**kwargs)

        def get_gradients(self, loss, params):
            """
            Compute gradients of all trainable variables.
            See Optimizer.get_gradients() for more info.
            In DistributedOptimizer, get_gradients() is overriden to also
            push_pull the gradients before returning them.
            """
            gradients = super(self.__class__, self).get_gradients(loss, params)
            return self._push_pull(gradients)

        def _aggregate_gradients(self, grads_and_vars):
            gradients = [grad for grad, var in grads_and_vars]
            return self._push_pull(gradients)

        def _push_pull_tf(self, gradients):
            self._aggregated_gradients = True
            if bps.size() > 1:
                averaged_gradients = []
                with tf.name_scope(self._name + "_Push_Pull") as scope:
                    for grad in gradients:
                        if grad is not None:
                            if self._sparse_as_dense and \
                                    isinstance(grad, tf.IndexedSlices):
                                grad = tf.convert_to_tensor(grad)
                            avg_grad = bps.push_pull(grad, scope,
                                                     device_dense=self._device_dense,
                                                     device_sparse=self._device_sparse,
                                                     compression=self._compression)
                            averaged_gradients.append(avg_grad)
                        else:
                            averaged_gradients.append(None)
                    return averaged_gradients
            else:
                return gradients

        def _push_pull_xla(self, grads):
            for item in grads:
                print("xxxxxxxxxxxxxx x2582", item)
            self._aggregated_gradients = True
            if bps.size() <= 1:
                return grads

            with tf.name_scope(self._name + "_Push_Pull") as scope:
                if self._sparse_as_dense:
                    grads = [tf.convert_to_tensor(grad)
                             if grad is not None and isinstance(grad, tf.IndexedSlices)
                             else grad for grad in grads]
                new_grads_names_and_handles_and_ctxes = \
                    [push_pull_xla_handle_out_v2(grad, scope,
                        device_dense=self._device_dense,
                        device_sparse=self._device_sparse,
                        compression=self._compression, idx = idx)
                     if grad is not None else grad
                     for idx, grad in enumerate(grads, 1)]

                with tf.device(self._device_dense):
                    grads_and_names_and_handles_and_ctxes = list(zip(*new_grads_names_and_handles_and_ctxes))
                    avg_grads, grad_names, handles, ctxes = \
                      list(grads_and_names_and_handles_and_ctxes[0]), \
                      list(grads_and_names_and_handles_and_ctxes[1]), \
                      list(grads_and_names_and_handles_and_ctxes[2]), \
                      list(grads_and_names_and_handles_and_ctxes[3])

                    barrier_handle = _my_barrier_handle_out(handles)
                    avg_grads = [_sync_tensors_handle_out_v2(tensor, barrier_handle, tensor_name=item, idx = idx) for idx, (tensor, item) in enumerate(zip(avg_grads, grad_names), 1)]
                    avg_grads = [self._compression.decompress(item, ctx) for item, ctx in zip(avg_grads, ctxes)]
            return avg_grads

        def apply_gradients(self, *args, **kwargs):
            if not self._aggregated_gradients:
                raise Exception('`apply_gradients()` was called without a call to '
                                '`get_gradients()` or `_aggregate_gradients`. If you\'re '
                                'using TensorFlow 2.0, please specify '
                                '`experimental_run_tf_function=False` in `compile()`.')
            return super(self.__class__, self).apply_gradients(*args, **kwargs)

    # We dynamically create a new class that inherits from the optimizer that was passed in.
    # The goal is to override get_gradients() method with an push_pull implementation.
    # This class will have the same name as the optimizer it's wrapping, so that the saved
    # model could be easily restored without BytePS.
    cls = type(optimizer.__class__.__name__, (optimizer.__class__,),
               dict(_DistributedOptimizer.__dict__))
    return cls.from_config(optimizer.get_config())


def _eval(backend, op_or_result):
    if bps._executing_eagerly():
        return op_or_result
    else:
        return backend.get_session().run(op_or_result)


if hasattr(bps, 'broadcast_global_variables'):
    def broadcast_global_variables(backend, root_rank):
        return _eval(backend, bps.broadcast_global_variables(root_rank))


def push_pull(backend, value, name, average):
    return _eval(backend,  bps.push_pull(tf.constant(value, name=name), average=average))


def broadcast(backend, value, root_rank, name):
    return _eval(backend, bps.broadcast(tf.constant(value, name=name), root_rank, is_variable=False))


def load_model(keras, wrap_optimizer, optimizer_modules, filepath, custom_optimizers, custom_objects):
    byteps_objects = {
        subclass.__name__.lower(): wrap_optimizer(subclass)
        for subclass in keras.optimizers.Optimizer.__subclasses__()
        if subclass.__module__ in optimizer_modules
    }

    if custom_optimizers is not None:
        byteps_objects.update({
            cls.__name__: wrap_optimizer(cls)
            for cls in custom_optimizers
        })

    if custom_objects is not None:
        byteps_objects.update(custom_objects)

    return keras.models.load_model(filepath, custom_objects=byteps_objects)
