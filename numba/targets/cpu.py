from __future__ import print_function, absolute_import

import sys

import llvmlite.llvmpy.core as lc
import llvmlite.llvmpy.ee as le
import llvmlite.binding as ll

from numba import _dynfunc, config
from numba.callwrapper import PyCallWrapper
from .base import BaseContext, PYOBJECT
from numba import utils, cgutils, types
from numba.utils import cached_property
from numba.targets import (
    callconv, codegen, externals, intrinsics, cmathimpl, mathimpl,
    npyimpl, operatorimpl, printimpl, randomimpl)
from .options import TargetOptions


# Keep those structures in sync with _dynfunc.c.

class ClosureBody(cgutils.Structure):
    _fields = [('env', types.pyobject)]


class EnvBody(cgutils.Structure):
    _fields = [
        ('globals', types.pyobject),
        ('consts', types.pyobject),
    ]


class CPUContext(BaseContext):
    """
    Changes BaseContext calling convention
    """
    # Overrides
    def create_module(self, name):
        return self._internal_codegen._create_empty_module(name)

    def init(self):
        self.native_funcs = utils.UniqueDict()
        self.is32bit = (utils.MACHINE_BITS == 32)

        # Map external C functions.
        externals.c_math_functions.install()
        externals.c_numpy_functions.install()

        # Add target specific implementations
        self.install_registry(cmathimpl.registry)
        self.install_registry(mathimpl.registry)
        self.install_registry(npyimpl.registry)
        self.install_registry(operatorimpl.registry)
        self.install_registry(printimpl.registry)
        self.install_registry(randomimpl.registry)

        self._internal_codegen = codegen.JITCPUCodegen("numba.exec")

    @property
    def target_data(self):
        return self._internal_codegen.target_data

    def aot_codegen(self, name):
        return codegen.AOTCPUCodegen(name)

    def jit_codegen(self):
        return self._internal_codegen

    @cached_property
    def call_conv(self):
        return callconv.CPUCallConv(self)

    def get_env_from_closure(self, builder, clo):
        """
        From the pointer *clo* to a _dynfunc.Closure, get a pointer
        to the enclosed _dynfunc.Environment.
        """
        clo_body_ptr = cgutils.pointer_add(
            builder, clo, _dynfunc._impl_info['offset_closure_body'])
        clo_body = ClosureBody(self, builder, ref=clo_body_ptr, cast_ref=True)
        return clo_body.env

    def get_env_body(self, builder, envptr):
        """
        From the given *envptr* (a pointer to a _dynfunc.Environment object),
        get a EnvBody allowing structured access to environment fields.
        """
        body_ptr = cgutils.pointer_add(
            builder, envptr, _dynfunc._impl_info['offset_env_body'])
        return EnvBody(self, builder, ref=body_ptr, cast_ref=True)

    def remove_native_function(self, func):
        """
        Remove internal references to nonpython mode function *func*.
        KeyError is raised if the function isn't known to us.
        """
        del self.native_funcs[func]

    def post_lowering(self, func):
        mod = func.module

        if (sys.platform.startswith('linux') or
                sys.platform.startswith('win32')):
            intrinsics.fix_powi_calls(mod)

        if self.is32bit:
            # 32-bit machine needs to replace all 64-bit div/rem to avoid
            # calls to compiler-rt
            intrinsics.fix_divmod(mod)

    def create_cpython_wrapper(self, library, fndesc, call_helper,
                               release_gil=False):
        wrapper_module = self.create_module("wrapper")
        fnty = self.call_conv.get_function_type(fndesc.restype, fndesc.argtypes)
        wrapper_callee = wrapper_module.add_function(fnty, fndesc.llvm_func_name)
        builder = PyCallWrapper(self, wrapper_module, wrapper_callee,
                                fndesc, call_helper=call_helper,
                                release_gil=release_gil)
        builder.build()
        library.add_ir_module(wrapper_module)

    def get_executable(self, library, fndesc, env):
        """
        Returns
        -------
        (cfunc, fnptr)

        - cfunc
            callable function (Can be None)
        - fnptr
            callable function address
        - env
            an execution environment (from _dynfunc)
        """
        func = library.get_function(fndesc.llvm_func_name)
        wrapper = library.get_function(fndesc.llvm_cpython_wrapper_name)

        # Code generation
        baseptr = library.get_pointer_to_function(func.name)
        fnptr = library.get_pointer_to_function(wrapper.name)

        cfunc = _dynfunc.make_function(fndesc.lookup_module(),
                                       fndesc.qualname.split('.')[-1],
                                       fndesc.doc, fnptr, env)

        if fndesc.native:
            self.native_funcs[cfunc] = fndesc.llvm_func_name, baseptr

        return cfunc

    def calc_array_sizeof(self, ndim):
        '''
        Calculate the size of an array struct on the CPU target
        '''
        aryty = types.Array(types.int32, ndim, 'A')
        return self.get_abi_sizeof(self.get_value_type(aryty))


# ----------------------------------------------------------------------------
# TargetOptions

class CPUTargetOptions(TargetOptions):
    OPTIONS = {
        "nopython": bool,
        "nogil": bool,
        "forceobj": bool,
        "looplift": bool,
        "wraparound": bool,
        "boundcheck": bool,
    }


# ----------------------------------------------------------------------------
# Internal

def remove_refct_calls(func):
    """
    Remove redundant incref/decref within on a per block basis
    """
    for bb in func.basic_blocks:
        remove_null_refct_call(bb)
        remove_refct_pairs(bb)


def remove_null_refct_call(bb):
    """
    Remove refct api calls to NULL pointer
    """
    pass
    ## Skipped for now
    # for inst in bb.instructions:
    #     if isinstance(inst, lc.CallOrInvokeInstruction):
    #         fname = inst.called_function.name
    #         if fname == "Py_IncRef" or fname == "Py_DecRef":
    #             arg = inst.args[0]
    #             print(type(arg))
    #             if isinstance(arg, lc.ConstantPointerNull):
    #                 inst.erase_from_parent()


def remove_refct_pairs(bb):
    """
    Remove incref decref pairs on the same variable
    """

    didsomething = True

    while didsomething:
        didsomething = False

        increfs = {}
        decrefs = {}

        # Mark
        for inst in bb.instructions:
            if isinstance(inst, lc.CallOrInvokeInstruction):
                fname = inst.called_function.name
                if fname == "Py_IncRef":
                    arg = inst.operands[0]
                    increfs[arg] = inst
                elif fname == "Py_DecRef":
                    arg = inst.operands[0]
                    decrefs[arg] = inst

        # Sweep
        for val in increfs.keys():
            if val in decrefs:
                increfs[val].erase_from_parent()
                decrefs[val].erase_from_parent()
                didsomething = True
