#!/usr/bin/env python
# -*- coding: utf-8 -*-
# File: training.py

from abc import ABCMeta, abstractmethod
import tensorflow as tf
import six
from six.moves import zip, range

from ..utils import logger
from ..utils.argtools import memoized
from ..tfutils.gradproc import FilterNoneGrad
from ..tfutils.tower import TowerContext, get_current_tower_context
from ..tfutils.common import get_tf_version_number
from ..tfutils.collection import backup_collection, restore_collection
from ..tfutils.gradproc import ScaleGradient
from ..utils.naming import TOWER_FREEZE_KEYS
from .utils import LeastLoadedDeviceSetter, override_to_local_variable


__all__ = ['GraphBuilder', 'SimpleBuilder',
           'SyncMultiGPUParameterServerBuilder', 'DataParallelBuilder',
           'SyncMultiGPUReplicatedBuilder', 'AsyncMultiGPUBuilder']


@six.add_metaclass(ABCMeta)
class GraphBuilder(object):
    @abstractmethod
    def build(*args, **kwargs):
        pass


class SimpleBuilder(GraphBuilder):
    """
    Build the graph for single-cost single-optimizer single-tower training.
    """
    def build(self, input, get_cost_fn, get_opt_fn):
        """
        Args:
            input (InputSource): the input. Should have been setup.
            get_cost_fn ([tf.Tensor] -> tf.Tensor): callable which takes a list of input tensor
                and returns a cost tensor
            get_opt_fn (-> tf.train.Optimizer): callable which returns an optimizer

        Returns:
            tf.Operation: the training op
        """
        assert input.setup_done()
        with TowerContext('', is_training=True) as ctx:
            cost = get_cost_fn(*input.get_input_tensors())

            varlist = ctx.filter_vars_by_vs_name(tf.trainable_variables())
            opt = get_opt_fn()
            grads = opt.compute_gradients(
                cost, var_list=varlist,
                gate_gradients=False, colocate_gradients_with_ops=True)
            grads = FilterNoneGrad().process(grads)
            train_op = opt.apply_gradients(grads, name='min_op')
            return train_op


class DataParallelBuilder(GraphBuilder):
    def __init__(self, towers):
        """
        Args:
            towers(list[int]): list of GPU ids.
        """
        if len(towers) > 1:
            logger.info("Training a model of {} towers".format(len(towers)))
            DataParallelBuilder._check_tf_version()

        self.towers = towers

    @staticmethod
    def _check_tf_version():
        assert get_tf_version_number() >= 1.1, \
            "TF version {} is too old to run multi GPU training!".format(tf.VERSION)

    @staticmethod
    def _check_grad_list(grad_list):
        """
        Args:
            grad_list: list of list of tuples, shape is Ngpu x Nvar x 2
        """
        nvars = [len(k) for k in grad_list]
        assert len(set(nvars)) == 1, "Number of gradients from each tower is different! " + str(nvars)

    @staticmethod
    def build_on_towers(
            towers, func, devices=None, use_vs=None):
        """
        Run `func` on all GPUs (towers) and return the results.

        Args:
            towers (list[int]): a list of GPU id.
            func: a lambda to be called inside each tower
            devices: a list of devices to be used. By default will use '/gpu:{tower}'
            use_vs (list[bool]): list of use_vs to passed to TowerContext

        Returns:
            List of outputs of ``func``, evaluated on each tower.
        """

        ret = []
        if devices is not None:
            assert len(devices) == len(towers)
        if use_vs is not None:
            assert len(use_vs) == len(towers)

        tower_names = ['tower{}'.format(idx) for idx in range(len(towers))]

        for idx, t in enumerate(towers):
            device = devices[idx] if devices is not None else '/gpu:{}'.format(t)
            usevs = use_vs[idx] if use_vs is not None else False
            with tf.device(device), TowerContext(
                    tower_names[idx],
                    is_training=True,
                    index=idx,
                    use_vs=usevs):
                logger.info("Building graph for training tower {} on device {}...".format(idx, device))

                # When use_vs is True, use LOCAL_VARIABLES,
                # so these duplicated variables won't be saved by default.
                with override_to_local_variable(enable=usevs):
                    ret.append(func())

                if idx == 0:
                    # avoid duplicated summary & update_ops from each device
                    backup = backup_collection(TOWER_FREEZE_KEYS)
        restore_collection(backup)
        return ret

    @staticmethod
    def _make_fn(input, get_cost_fn, get_opt_fn):
        # internal use only
        assert input.setup_done()
        get_opt_fn = memoized(get_opt_fn)

        def get_grad_fn():
            ctx = get_current_tower_context()
            cost = get_cost_fn(*input.get_input_tensors())

            varlist = ctx.filter_vars_by_vs_name(tf.trainable_variables())
            opt = get_opt_fn()
            grads = opt.compute_gradients(
                cost, var_list=varlist,
                gate_gradients=False, colocate_gradients_with_ops=True)
            grads = FilterNoneGrad().process(grads)
            return grads

        return get_grad_fn, get_opt_fn


class SyncMultiGPUParameterServerBuilder(DataParallelBuilder):
    """
    Graph builder for data-parallel training in 'ParameterServer' mode.
    It builds one tower on each GPU with
    shared variable scope. It synchronoizes the gradients computed
    from each tower, averages them and applies to the shared variables.

    See https://www.tensorflow.org/performance/benchmarks for details.
    """
    def __init__(self, towers, ps_device=None):
        """
        Args:
            towers(list[int]): list of GPU id
            ps_device (str): either 'gpu' or 'cpu', where variables are stored.
                Setting to 'cpu' might help when #gpu>=4
        """
        super(SyncMultiGPUParameterServerBuilder, self).__init__(towers)
        if ps_device is None:
            ps_device = 'cpu' if len(towers) >= 4 else 'gpu'
        assert ps_device in ['cpu', 'gpu']
        self.ps_device = ps_device

    @staticmethod
    def _average_grads(tower_grads):
        # tower_grads: Ngpu x Nvar x 2
        nr_tower = len(tower_grads)
        if nr_tower == 1:
            return tower_grads[0]
        new_tower_grads = []
        with tf.name_scope('AvgGrad'):
            for grad_and_vars in zip(*tower_grads):
                # Ngpu * 2
                v = grad_and_vars[0][1]
                all_grads = [g for (g, _) in grad_and_vars]

                with tf.device(v.device):       # colocate summed grad with var
                    grad = tf.multiply(
                        tf.add_n(all_grads), 1.0 / nr_tower)
                    new_tower_grads.append((grad, v))
        return new_tower_grads

    def build(self, input, get_cost_fn, get_opt_fn):
        """
        Args:
            input (InputSource):
            get_cost_fn ([tf.Tensor] -> tf.Tensor): callable which takes a list of input tensor
                and returns a cost tensor
            get_opt_fn (-> tf.train.Optimizer): callable which returns an optimizer

        Returns:
            tf.Operation: the training op
        """
        raw_devices = ['/gpu:{}'.format(k) for k in self.towers]
        if self.ps_device == 'gpu':
            devices = [LeastLoadedDeviceSetter(d, raw_devices) for d in raw_devices]
        else:
            devices = [tf.train.replica_device_setter(
                worker_device=d, ps_device='/cpu:0', ps_tasks=1) for d in raw_devices]

        get_grad_fn, get_opt_fn = DataParallelBuilder._make_fn(input, get_cost_fn, get_opt_fn)

        grad_list = DataParallelBuilder.build_on_towers(self.towers, get_grad_fn, devices)
        DataParallelBuilder._check_grad_list(grad_list)

        # debug tower performance (without update):
        # ops = [k[0] for k in grad_list[1]] + [k[0] for k in grad_list[0]]
        # self.train_op = tf.group(*ops)
        # return

        grads = SyncMultiGPUParameterServerBuilder._average_grads(grad_list)
        # grads = grad_list[0]

        opt = get_opt_fn()
        if self.ps_device == 'cpu':
            with tf.device('/cpu:0'):
                train_op = opt.apply_gradients(grads, name='train_op')
        else:
            train_op = opt.apply_gradients(grads, name='train_op')
        return train_op


class SyncMultiGPUReplicatedBuilder(DataParallelBuilder):
    """
    Graph builder for data-parallel training in "replicated" mode,
    where each GPU contains a replicate of the whole model.
    It will build one tower on each GPU under its own variable scope.
    Each gradient update is averaged across or GPUs through NCCL.

    See https://www.tensorflow.org/performance/benchmarks for details.
    """
    @staticmethod
    def _allreduce_grads(tower_grads):
        from tensorflow.contrib import nccl
        nr_tower = len(tower_grads)
        if nr_tower == 1:
            return [[x] for x in tower_grads[0]]
        new_tower_grads = []
        with tf.name_scope('AvgGrad'):
            for grad_and_vars in zip(*tower_grads):
                v = grad_and_vars[0][1]
                grads = [g for g, _ in grad_and_vars]
                summed = nccl.all_sum(grads)

                grads_for_a_var = []
                for (_, v), g in zip(grad_and_vars, summed):
                    with tf.device(g.device):
                        g = tf.multiply(g, 1.0 / nr_tower)
                        grads_for_a_var.append((g, v))
                new_tower_grads.append(grads_for_a_var)
        # NVar * NGPU * 2
        return new_tower_grads

    def build(self, input, get_cost_fn, get_opt_fn):
        """
        Args:
            input (InputSource): the input. Should have been setup.
            get_cost_fn ([tf.Tensor] -> tf.Tensor): callable which takes a list of input tensor
                and returns a cost tensor
            get_opt_fn (-> tf.train.Optimizer): callable which returns an optimizer

        Returns:
            (tf.Operation, tf.Operation)

            1. the training op.

            2. the op which sync variables from GPU 0 to other GPUs.
                It has to be run before the training has started.
                And you can optionally run it later to sync non-trainable variables.
        """
        raw_devices = ['/gpu:{}'.format(k) for k in self.towers]

        get_grad_fn, get_opt_fn = DataParallelBuilder._make_fn(input, get_cost_fn, get_opt_fn)

        grad_list = DataParallelBuilder.build_on_towers(
            self.towers,
            get_grad_fn,
            # use no variable scope for the first tower
            use_vs=[False] + [True] * (len(self.towers) - 1))
        grads = SyncMultiGPUReplicatedBuilder._allreduce_grads(grad_list)

        train_ops = []
        opt = get_opt_fn()
        for idx in range(len(self.towers)):
            with tf.device(raw_devices[idx]):
                grad_and_vars = [x[idx] for x in grads]
                # apply_gradients may create variables. Make them LOCAL_VARIABLES
                with override_to_local_variable(enable=idx > 0):
                    train_ops.append(opt.apply_gradients(
                        grad_and_vars, name='apply_grad_{}'.format(idx)))
        train_op = tf.group(*train_ops, name='train_op')
        post_init_op = SyncMultiGPUReplicatedBuilder.get_post_init_ops()
        return train_op, post_init_op

# Adopt from https://github.com/tensorflow/benchmarks/blob/master/scripts/tf_cnn_benchmarks/variable_mgr.py
    @staticmethod
    def get_post_init_ops():
        """
        Copy values of variables on GPU 0 to other GPUs.
        """
        # literally all variables, because it's better to sync optimizer-internal variables as well
        all_vars = tf.global_variables() + tf.local_variables()
        var_by_name = dict([(v.name, v) for v in all_vars])
        post_init_ops = []
        for v in all_vars:
            if not v.name.startswith('tower'):
                continue
            if v.name.startswith('tower0'):
                logger.warn("[SyncMultiGPUReplicatedBuilder] variable "
                            "{} has prefix 'tower0', this is unexpected.".format(v.name))
                continue        # TODO some vars (EMA) may still startswith tower0
            # in this trainer, the master name doesn't have the towerx/ prefix
            split_name = v.name.split('/')
            prefix = split_name[0]
            realname = '/'.join(split_name[1:])
            if prefix in realname:
                logger.error("[SyncMultiGPUReplicatedBuilder] variable "
                             "{} has its prefix {} appears multiple times in its name!".format(v.name, prefix))
            copy_from = var_by_name.get(realname)
            assert copy_from is not None, var_by_name.keys()
            post_init_ops.append(v.assign(copy_from.read_value()))
        logger.info(
            "'sync_variables_from_main_tower' includes {} operations.".format(len(post_init_ops)))
        return tf.group(*post_init_ops, name='sync_variables_from_main_tower')


class AsyncMultiGPUBuilder(DataParallelBuilder):
    """
    Graph builder for data-parallel training with async update.
    It builds one tower on each GPU with shared variable scope.
    Every tower computes the gradients and independently applies them to the
    variables, without synchronizing and averaging across towers.
    """

    def __init__(self, towers, scale_gradient=True):
        """
        Args:
            towers(list[int]): list of GPU ids.
            scale_gradient (bool): if True, will scale each gradient by ``1.0/nr_gpu``.
        """
        super(AsyncMultiGPUBuilder, self).__init__(towers)
        self._scale_gradient = scale_gradient

    def build(self, input, get_cost_fn, get_opt_fn):
        """
        Args:
            input (InputSource): the input. Should have been setup.
            get_cost_fn ([tf.Tensor] -> tf.Tensor): callable which takes a list of input tensor
                and returns a cost tensor
            get_opt_fn (-> tf.train.Optimizer): callable which returns an optimizer

        Returns:
            tf.Operation: the training op
        """
        ps_device = 'cpu' if len(self.towers) >= 4 else 'gpu'

        if ps_device == 'gpu':
            raw_devices = ['/gpu:{}'.format(k) for k in self.towers]
            devices = [LeastLoadedDeviceSetter(d, raw_devices) for d in raw_devices]
        else:
            devices = [tf.train.replica_device_setter(
                worker_device=d, ps_device='/cpu:0', ps_tasks=1) for d in raw_devices]

        get_grad_fn, get_opt_fn = DataParallelBuilder._make_fn(input, get_cost_fn, get_opt_fn)

        grad_list = DataParallelBuilder.build_on_towers(self.towers, get_grad_fn, devices)
        DataParallelBuilder._check_grad_list(grad_list)

        if self._scale_gradient and len(self.towers) > 1:
            # pretend to average the grads, in order to make async and
            # sync have consistent effective learning rate
            gradproc = ScaleGradient(('.*', 1.0 / len(self.towers)), verbose=False)
            grad_list = [gradproc.process(gv) for gv in grad_list]
        # Ngpu x Nvar x 2

        train_ops = []
        opt = get_opt_fn()
        with tf.name_scope('async_apply_gradients'):
            for i, grad_and_vars in enumerate(zip(*grad_list)):
                # Ngpu x 2
                v = grad_and_vars[0][1]
                with tf.device(v.device):
                    # will call apply_gradients (therefore gradproc) multiple times
                    train_ops.append(opt.apply_gradients(
                        grad_and_vars, name='apply_grad_{}'.format(i)))
            return tf.group(*train_ops, name='train_op')
