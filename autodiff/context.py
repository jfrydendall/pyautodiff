import logging
import meta
from ast import *
import types
import inspect
import numpy as np
import theano
import theano.tensor as T
import autodiff.utils as utils
import autodiff.functions


logger = logging.getLogger('autodiff')

# XXX FIXME This will not do - seed must be exposed.
# from theano.sandbox.rng_mrg import MRG_RandomStreams as RandomStreams
from theano.tensor.shared_randomstreams import RandomStreams
global_randomstreams = RandomStreams(seed=12345)#np.random.randint(1, 999999))


def get_ast(fn):
    fn_def = meta.decompiler.decompile_func(fn)
    if isinstance(fn_def, Lambda):
        fn_def = FunctionDef(name='<lambda>',
                             args=fn_def.args,
                             body=[Return(fn_def.body)],
                             decorator_list=[])

    # Meta gets these fields wrong...
    argspec = inspect.getargspec(fn)
    fn_def.args.vararg = argspec.varargs
    fn_def.args.kwarg = argspec.keywords

    return fn_def


def get_source(ast):
    if hasattr(ast, 'func_code'):
        ast = get_ast(ast)
    elif callable(ast):
        ast = get_ast(ast.__call__)
    return meta.asttools.dump_python_source(ast)


def print_ast(ast):
    if hasattr(ast, 'func_code'):
        ast = get_ast(ast)
    elif callable(ast):
        ast = get_ast(ast.__call__)
    meta.asttools.print_ast(ast)


def print_source(ast):
    if hasattr(ast, 'func_code'):
        ast = get_ast(ast)
    elif callable(ast):
        ast = get_ast(ast.__call__)
    meta.asttools.python_source(ast)




def simple_Call(func, args=None):
    """
    Simple alias for building Call nodes that doesn't require specification of
    keywords, kwargs or starargs.
    """
    args = utils.as_seq(args)
    call = Call(args=args,
                func=func,
                keywords=[],
                kwargs=None,
                starargs=None)
    return call


def isvar_ast(name):
    """
    Wraps a Name node in a call to utils.isvar.
    """
    isvar = simple_Call(args=utils.as_seq(name),
                        func=Attribute(attr='isvar',
                                       ctx=Load(),
                                       value=Name(ctx=Load(), id='___utils')))
    return isvar



class Context(object):

    def __init__(self, borrowable=()):
        self.s_vars = dict()
        self.tags = dict()
        # FIXME do we need to hold on to all of these itermediates?
        # ensure these id's do not get recycled by garbage collection
        self._nogc = []
        self._noshadow = set()
        self._top_def = None
        self.borrowable = [id(b) for b in borrowable]

    def recompile(self, f, nested=False):
        """
        Accepts a function f that operates on numerical objects and
        returns a function that operates on Theano objects.

        nested : bool
            `recompile` resets the context and sets the 'top_node' of the
            function, which helps in tracing arguments. By passing nested=True,
            this reset can be bypassed. This is used, for example, when
            transforming nested functions. In this case, we want to use the
            same context but keep it when calling recompile.
        """
        transformer = TheanoTransformer(context=self)

        f_ast = get_ast(f)

        if not nested:
            self._top_def = f_ast
            self.tags.clear()

        transformed_ast = fix_missing_locations(transformer.visit(f_ast))

        f_globals = f.func_globals.copy()
        f_globals.update(dict(___ctx=transformer,
                              ___functions=autodiff.functions,
                              ___T=theano.tensor,
                              ___utils=autodiff.utils))
        if f.func_closure:
            f_globals.update((v, transformer.shadow(c.cell_contents)) for v, c in
                             zip(f.func_code.co_freevars, f.func_closure))

        for name in f.func_code.co_names:
            if name in f_globals.iterkeys():
                f_globals[name] = transformer.shadow(f_globals[name])

        new_f = meta.decompiler.compile_func(ast_node=transformed_ast,
                                             filename='<Context-AST>',
                                             globals=f_globals)

        # recreate method, if necessary
        if isinstance(f, types.MethodType):
            new_f = types.MethodType(new_f, f.im_self, f.im_class)

        # add defaults, if necessary (meta erases them and won't recompile!)
        if f.func_defaults:
            new_f.func_defaults = utils.clean_int_args(*f.func_defaults)[0]

        return new_f


    def get_symbolic(self, x):
        """
        Attempts to retrieve the symbolic version of x.

        if x is an numeric object (int, float, numpy array), it must have been
        traced by the context during recompiled function execution.

        if x is a string, it must have been tagged with
        autodiff.functions.tag().
        """
        if isinstance(x, basestring):
            if x in self.s_vars:
                return self.s_vars[x]
            elif x in self.tags:
                return self.tags[x]
            else:
                raise ValueError(
                    'Requested the symbolic variable of tag `{0}`'
                    ', but `{0}` was not tagged.'.format(x))
        elif utils.isvar(x):
            return x
        elif id(x) in self.s_vars:
            return self.s_vars[id(x)]
        elif isinstance(x, int) and -5 <= x <= 256:
            raise ValueError(
                'Small integers (-5 <= x <= 256) can not be shadowed due to '
                'CPython caching. Try casting the variable as a NumPy int type '
                'or array before tracing: {0}'.format(x))
        else:
            raise ValueError(
                'Requested the symbolic variable shadowing object {0}'
                ', but it was not traced.'.format(repr(x)))

    def reset(self):
        self.s_vars.clear()
        self.tags.clear()
        self._nogc = []
        self._noshadow = set()
        self._top_node = None


class ShadowClass(object):
    """
    A class that [almost] transparently wraps other objects, shadowing any
    requested attributes and function calls. Attributes can not be set.
    """
    __wraps___ = None
    __ignore___ = ['__class__',
                  '__mro__',
                  '__repr__',
                  '__str__',
                  '__new__',
                  '__init__',
                  '__dict__',
                  '__call__',
                  '__name__',
                  '__setattr__',
                  '__getattr__',
                  '__getattribute__',
                  '__shadow__']

    def __init__(self, obj, context):
        if isinstance(obj,  type):
            raise TypeError()
        if self.__wraps___ is None:
            raise TypeError('__wraps__ not set.')
        assert isinstance(obj, self.__wraps___)
        self.__dict__['_obj__'] = obj
        self.__dict__['_transformer__'] = TheanoTransformer(context)

    def __setattr__(SetComp, name, value):
         raise TypeError(
            'To protect code integrity, attributes of shadowed objects '
            'can not be set: Shadowed {0}'.format(self._obj__))

    def __getattr__(self, name):
        attr = getattr(self._obj__, name)
        return self._transformer__.shadow(attr)

    def __call__(self, *args, **kwargs):
        handle_functions = self._transformer__.handle_functions
        rval = handle_functions(self._obj__).__call__(*args, **kwargs)
        return self._transformer__.shadow(rval)

    class __metaclass__(type):
        def __init__(cls, name, bases, dct):

            def make_proxy(name):
                def proxy(self, *args):
                    print name
                    return self._transformer__.shadow(getattr(self._obj__, name))
                return proxy

            type.__init__(cls, name, bases, dct)
            if cls.__wraps___:
                for name in dir(cls.__wraps___):
                    if name.startswith("__"):
                        if (name not in cls.__ignore___
                            and name not in dct):
                            attr = getattr(cls.__wraps___, name, None)
                            try:
                                setattr(cls, name, property(make_proxy(name)))
                            except:
                                pass

class TheanoTransformer(NodeTransformer):

    def __init__(self, context):
        super(TheanoTransformer, self).__init__()
        self.context = context

    def ast_wrap(self, method_name, args):
        """
        Allows Python methods to be applied to AST nodes at runtime.

        `method_name` is a method of the TheanoTransformer class that accepts
        Python objects as arguments.

        `args` are the AST nodes representing the arguments for `method_name`
        (not including `self`!).

        ast_wrap returns an `ast.Call()` node which calls the method on the
        specified arguments at runtime.
        """
        wrapped = simple_Call(func=Attribute(attr=method_name,
                                              ctx=Load(),
                                              value=Name(ctx=Load(),
                                                         id='___ctx')),
                               args=args)

        return wrapped

    # ** --------------------------------------------------------
    # ** Direct Manipulation (Methods)

    def shadow(self, args):
        """
        Helper function for `_shadow` that calls it on a flattened version of
        its argument.
        """
        shadow_vars = [self._shadow_inner(x) for x in utils.flatten(args)]
        return utils.unflatten(args, shadow_vars)

    def _shadow_inner(self, x):

        """
        Given a numerical variable x, return an equivalent Theano shared
        variable and store the relationship in self.s_vars. Otherwise return x.
        """
        if (id(x) in self.context._noshadow
            or x is True
            or x is False
            or x is None):
            return x

        if utils.isvar(x):
            return x

        if isinstance(x, (int, float, np.number, np.ndarray)):
            # take special care with small ints, because CPython caches them.
            if isinstance(x, int) and -5 <= x <= 256:
                x = np.int_(x)

            if getattr(x, 'dtype', None) == bool:
                logger.info('Warning: Theano has no bool type; upgrading to int8.')
                x = x.astype('int8')

            if id(x) not in self.context.s_vars:
                # add to _nogc to ensure that the id won't be reused
                self.context._nogc.append(x)
                # create symbolic version:
                if isinstance(x, np.ndarray) and id(x) in self.context.borrowable:
                    sym_x = theano.shared(x, borrow=True)
                else:
                    sym_x = theano.shared(x)
                # store symbolic version
                self.context.s_vars[id(x)] = sym_x
                # return symbolic version
                return sym_x
            else:
                return self.context.s_vars[id(x)]
        else:
            if (isinstance(x, types.FunctionType)
                    and inspect.getmodule(x) is autodiff.functions):
                return x
            elif isinstance(x, (type, ShadowClass)):
                return x
            else:
                class Shadow(ShadowClass):
                    __wraps___ = x.__class__
                return Shadow(x, self.context)

    @staticmethod
    def remove_shadow_class(x):
        def _remove_shadow_class(x):
            if isinstance(x, ShadowClass):
                return x._obj__
            else:
                return x
        return utils.unflatten(x, [_remove_shadow_class(i)
                                   for i in utils.flatten(x)])

    @staticmethod
    def escape(x):
        def _escape(x):
            if isinstance(x, theano.tensor.sharedvar.SharedVariable):
                return x.get_value()
            elif utils.isvar(x):
                try:
                    return x.eval()
                except:
                    raise ValueError('Could not escape {0}'.format(x))
            elif isinstance(x, ShadowClass):
                return x._obj__
            else:
                return x
        return utils.unflatten(x, [_escape(i) for i in utils.flatten(x)])

    def tag(self, obj, tag):
        if not isinstance(tag, basestring):
            raise ValueError('Tag must be a string. Received: {0}'.format(tag))
        if tag in self.context.tags:
            logger.warning(
                '{0} was tagged as {1}, but the tag {1} was already '
                'assigned. Note that the new tag will overwrite '
                'the old one.'.format(obj, tag))
        else:
            self.context.tags[tag] = obj
            if utils.isvar(obj):
                obj.name = tag
        return obj

    def tag_function_arg(self, obj, tag):
        """
        A version of tagging called only by visit_FunctionDef, which tags
        top-level function arguments and stores the tags in s_vars. These
        tags can not be overwritten.
        """
        self.context.s_vars[tag] = obj
        if utils.isvar(obj):
            obj.name = tag

    def handle_functions(self, func):
        """
        Given some function for, return another function.

        Generally used to exchange NumPy functions for Theano equivalents.
        """
        # ** ======================= first handle functions defined here!

        if isinstance(func, ShadowClass):
            return self.handle_functions(func._obj__)

        if getattr(func, '__module__', None) == __name__:
            return func

        # ** ======================= special autodiff functions

        elif func is autodiff.functions.escape:
            # escapes a variable from Tensor representation
            return self.escape

        elif func is autodiff.functions.escaped_call:
            # call a function on escaped arguments, without transforming the AST
            def escaped_call(fn, *args, **kwargs):
                esc_args = utils.unflatten(
                    args, [escape(a) for a in utils.flatten(args)])
                esc_kwargs = utils.unflatten(
                    kwargs, [escape(a) for a in utils.flatten(kwargs)])
                return fn(*esc_args, **esc_kwargs)
            return escaped_call

        elif func is autodiff.functions.tag:
            # tag a variable
            return self.tag

        # ** ======================= __theano_op__

        elif hasattr(func, '__theano_op__'):
            return func.__theano_op__

        # ** ======================= array methods (with tensor instances)

        elif utils.isvar(getattr(func, '__self__', None)):
            return self.handle_array_methods(func.__self__, func.__name__)

        # ** ======================= Theano function

        elif (getattr(func, '__module__', None)
              and getattr(func, '__module__').startswith('theano')):
            return func

        # ** ======================= type/casting functions

        elif type(func) is type:
            if func.__name__ in ['bool', 'bool_', 'bool8']:
                logger.info('Warning: Theano has no bool type; '
                            'upgrading to int8.')
                def bool_(x):
                    return T.neq(x, 0)
                return bool_
            elif func.__name__ in T.basic._cast_mapping.keys():
                def cast(x):
                    return T.cast(x, dtype=func.__name__)
                return cast
            elif func.__name__ == 'float':
                def float_(x):
                    return T.cast(x, dtype=theano.config.floatX)
                return float_
            elif func.__name__ == 'int':
                def int_(x):
                    return T.cast(x, dtype='int' + theano.config.floatX[-2:])
                return int_
            elif func.__name__ == 'enumerate':
                def enumerate_(iterable, start=0):
                    if utils.isvar(iterable):
                        raise TypeError(
                            'Called enumerate() on Tensor {0} but Tensors '
                            'do not support iteration. Maybe try escaping '
                            'the tensor?'.format(iterable))
                    else:
                        return enumerate(iterable, start=start)
                return enumerate_
            else:
                def new_type(*args, **kwargs):
                    try:
                        return self.shadow(func(*args, **kwargs))
                    except:
                        raise ValueError('Unsupported type: {0}'.format(func))
                return new_type

        # ** ======================= numpy functions

        elif (getattr(func, '__module__', None)
              and getattr(func, '__module__').startswith('numpy')
              or isinstance(func, np.ufunc)
              or str(func) == '<built-in function max>'
              or str(func) == '<built-in function min>'):

            # abs
            if func.__name__ in ('abs', 'absolute'):
                return abs

            # ones/zeros
            # FIXME submitted a PR to Theano to make syntax more
            # like Numpy; this change shouldn't be needed afterward.
            elif func.__name__ in ('ones', 'zeros'):
                def alloc(shp, dtype=None):
                    if (not isinstance(shp, (list, tuple))
                            and not utils.isvar(shp)):
                        shp = [shp]
                    return getattr(T, func.__name__)(shp, dtype)
                return alloc

            elif hasattr(T, func.__name__):
                return getattr(T, func.__name__)
            else:
                raise ValueError(
                    'Autodiff unsupported function: {0}'.format(func))

        # ** ======================= built-ins

        elif '<built-in' in str(func):

            # ranges
            if func.__name__ in ('range', 'xrange'):
                def range_(*args):
                    return func(*(self.escape(a) for a in args))
                return range_

            # zip
            elif func.__name__ == 'zip':
                def zip_(*args):
                    if __builtin__.any(utils.isvar(a) for a in args):
                        raise TypeError(
                            'Called zip() on Tensor but Tensors '
                            'do not support iteration. Maybe try escaping '
                            'the tensor?')
                    else:
                        return zip(*args)
                return zip_

            # uniform random numbers (np.random.uniform)
            elif 'method uniform of mtrand.RandomState' in str(func):
                def rand_u(low=0.0, high=1.0, size=1):
                    return global_randomstreams.uniform(low=low,
                                                        high=high,
                                                        size=size)
                return rand_u

            # standard uniform random numbers (np.random.random)
            elif ('method random of mtrand.RandomState' in str(func)
                  or 'method random_sample of mtrand.RandomState' in str(func)):
                def rand_u(size):
                    return global_randomstreams.uniform(size=size)
                return rand_u

            # normal random numbers (np.random.normal)
            elif 'method normal of mtrand.RandomState' in str(func):
                def rand_n(loc=0.0, scale=1.0, size=None):
                    return global_randomstreams.normal(avg=loc,
                                                       std=scale,
                                                       size=size)
                return rand_n

            # standard normal random numbers (np.random.randn)
            elif 'method randn of mtrand.RandomState' in str(func):
                def rand_n(*size):
                    return global_randomstreams.normal(size=size)
                return rand_n

            # binomial random numbers (np.random.binomial)
            elif 'method binomial randn of mtrand.RandomState' in str(func):
                def rand_b(n, p, size=None):
                    return global_randomstreams.binomial(n=n, p=p, size=size)
                return rand_b

            # isinstance
            elif func is isinstance:
                def isinstance_(obj, types):
                    escaped_obj = self.escape(obj)
                    if obj.ndim == 0:
                        escaped_obj = np.asscalar(escaped_obj)
                    return isinstance(escaped_obj, self.escape(types))
                return isinstance_

            # anything else (like getattr)
            else:
                return func

        # ** ======================= Misc

        elif (('ipdb' in getattr(func, '__module__', '')
              or 'pdb' in getattr(func, '__module__', ''))
              and func.__name__ == 'set_trace'):
            return func

        # ** ======================= Anything else

        else:
            try:
                return self.context.recompile(func, nested=True)
            except:
                raise ValueError('Unsupported function: {0}'.format(func))

        # ** ======================= Catchall (shouldn't be called)

        raise ValueError(
            'handle_functions: No case matched function {0}'.format(func))

    def handle_array_methods(self, var, method_name):
        """
        This method is called whenever:
            1. An array method is requested that doesn't exist for Theano
               variables (like _.swapaxes()). `handle_array_methods` is used
               to supply a replacement method. Note that in this case,
               `handle_array_methods` is called directly.
            2. A method is requested that DOES exist for Theano variables. In
               this case, `handle_array_methods` is called by `handle_functions`
               prior to calling the method. `handle_array_methods` is used to
               supply a replacement function that properly handles the supplied
               arguments (since they are compliant with the Numpy signature,
               not the Theano one).
        """
        # if we're not dealing with a Theano variable, nothing to do here.
        if not utils.isvar(var):
            return getattr(var, method_name)

        # Theano's reshape requires dim to be in a collection, unlike Numpy.
        if method_name == 'reshape':
            def reshape(*args, **kwargs):
                if not isinstance(args[0], (list, tuple)):
                    args = [args]
                return var.reshape(*args, **kwargs)
            return reshape

        # Theano has no swapaxes method
        elif method_name == 'swapaxes':
            def swapaxes(*args, **kwargs):
                axis1, axis2 = (self.escape(a) for a in args)
                dims = range(var.ndim)
                dims[axis1], dims[axis2] = dims[axis2], dims[axis1]
                return var.dimshuffle(*dims)
            return swapaxes

        # Theano doesn't process numpy dtype objects or 'bool'
        elif method_name == 'astype':
            def astype(*args, **kwargs):
                dtype = kwargs.pop('dtype', None)
                if not dtype:
                    dtype = args[0]
                if not isinstance(dtype, str):
                    # get numpy dtype objects like np.float32
                    try:
                        dtype = dtype.__name__
                    except:
                        raise NotImplementedError(
                            'Unsupported dtype: {0}'.format(dtype))
                if 'bool' in dtype:
                    dtype = 'int8'
                    logger.info('Warning: Theano has no bool type; '
                                'upgrading to int8.')
                return var.astype(dtype)
            return astype

        elif method_name == 'sort':
            def sort_(*args, **kwargs):
                raise ValueError(
                    'Calling an array\'s `sort()` method is not supported '
                    'because in NumPy it is an inplace operation, but in '
                    'Theano it is not. Please use numpy.sort() instead.')
            return sort_

        # ...Otherwise, try to access the method on the Theano variable
        else:
            return getattr(var, method_name)

    def handle_comparison(self, operator, left, right):
        """
        This method is called whenever an operator is encountered with a single
        rhs comparator, since tensors do not properly them.
        """
        if utils.isvar(left) or utils.isvar(right):
            return getattr(T, operator)(left, right)
        elif operator == 'gt':
            return left > right
        elif oeprator == 'ge':
            return left >= right
        elif operator == 'lt':
            return left < right
        elif operator == 'le':
            return left <= right
        elif operator == 'eq':
            return left == right
        elif operator == 'neq':
            return left != right
        else:
            # shouldn't ever reach here!
            raise ValueError(
                'Not sure how to handle operator: {0}'.format(operator))

    # ** --------------------------------------------------------
    # ** AST Manipulation (Node Visitors)

    def visit_Assign(self, node):
        """
        Applies the following transformations:

        - Transform subscripts. Tensor variables do not support inplace
          assignment, so subscript assigns must be changed to call the
          `set_subtensor` function.

            Statements of the form:
                x[a:b][c] = y
            Become:
                if utils.isvar(x):
                    x = T.set_subtensor(x[a:b], T.set_subtensor(x[a:b][c], y))
                else:
                    x[a:b][c] = y

        """
        self.generic_visit(node)

        # handle subscripted assignment for tensor variables
        if isinstance(node.targets[0], Subscript):

            # helper function to transform subscript into (possibly nested)
            # T.set_subtensor statements
            def build_subt(subscript, value):
                subscript_load = Subscript(ctx=Load(),
                                           slice=subscript.slice,
                                           value=subscript.value)
                set_subtensor = simple_Call(
                    args=[subscript_load, value],
                    func=Attribute(attr='set_subtensor',
                                   ctx=Load(),
                                   value=Name(ctx=Load(), id='___T')))
                if isinstance(subscript.value, Subscript):
                    set_subtensor = build_subt(subscript.value, set_subtensor)
                return set_subtensor

            # get root tensor; check for nested subscripts
            tensor = node.targets[0]
            while not isinstance(tensor, Name):
                tensor = tensor.value

            # transform subscript into set_subtensor
            set_subt = build_subt(subscript=node.targets[0], value=node.value)

            # wrap set_subtensor statements in Assign to root tensor
            assign_subtensor = Assign(targets=[Name(ctx=Store(), id=tensor.id)],
                                      value=set_subt)

            # wrap assign_subtensor in If to ensure that the modification
            # is only applied to tensor args
            check_var = If(test=isvar_ast(tensor),
                           body=[assign_subtensor],
                           orelse=[node])
            return check_var
        else:
            return node

    def visit_Attribute(self, node):
        self.generic_visit(node)
        new_node = simple_Call(args=[node.value,
                                Str(s=node.attr),
                                self.ast_wrap('handle_array_methods',
                                              [node.value, Str(s=node.attr)])],
                                func=Name(ctx=Load(), id='getattr'))
        return new_node

    def visit_AugAssign(self, node):
        """
        See documentation for self.visit_Assign() for information on
        transformations applied here.
        """

        if isinstance(node.target, Subscript):
            # apply op directly
            value = BinOp(left=Subscript(ctx=Load(),
                                         slice=node.target.slice,
                                         value=node.target.value),
                          right=node.value,
                          op=node.op)

            # farm out the work to visit_Assign
            assign_node = self.visit_Assign(Assign(targets=[node.target],
                                                   value=value))
            return assign_node
        else:
            self.generic_visit(node)
            return node

    def visit_Call(self, node):
        """
        Whenever a function is called, first pass it to the 'handle_functions'
        method. This method examines the function and modifies it prior to
        calling it. For example, it might replace `numpy.ones` with
        `theano.ones`.
        """
        self.generic_visit(node)
        node.func = self.ast_wrap('handle_functions', node.func)

        # the * and ** syntax won't work if an object has been shadowed...
        if node.starargs:
            node.starargs = self.ast_wrap('remove_shadow_class', node.starargs)
        if node.kwargs:
            node.kwargs = self.ast_wrap('remove_shadow_class', node.kwargs)

        return node

    def visit_Compare(self, node):
        """
        Replaces comparison operators with Theano functions, if either argument
        is a tensor variable. Prior to NumPy 1.8, this is required for all
        comparisons where the NumPy array is on the left; thereafter it is
        required only for == and !=.


        Given:

            x == y

        Becomes:

            ___ctx.handle_comparison('eq', x, y)

        Which internally performs:

            if utils.isvar(x) or utils.isvar(y):
                T.eq(x, y)
            else:
                x == y

        This could be done by directly replacing the literal comparison with
        the `if` clause, but this wouldn't be compatible with all code. For
        example, if the comparison takes place in an `if` clause, the new
        (and nested) `if` clause would be illegal syntax. Wrapping the `isvar`
        check in a function call means the syntax remains compatible.
        """
        self.generic_visit(node)

        if isinstance(node.ops[0], Eq):
            theano_op = Str(s='eq')
        elif isinstance(node.ops[0], NotEq):
            theano_op = Str(s='neq')
        elif isinstance(node.ops[0], Gt):
            theano_op = Str(s='gt')
        elif isinstance(node.ops[0], GtE):
            theano_op = Str(s='ge')
        elif isinstance(node.ops[0], Lt):
            theano_op = Str(s='lt')
        elif isinstance(node.ops[0], LtE):
            theano_op = Str(s='le')
        else:
            # Is, IsNot, In, NotIn
            return node

        if len(node.comparators) == 1:
            return self.ast_wrap('handle_comparison',
                                 [theano_op, node.left, node.comparators[0]])
        else:
            return node

    def visit_FunctionDef(self, node):
        """
        When a function is defined, shadow each of its arguments immediately.

        The AST is modified so that a function defined as:

            def f(a, b=None, *c, **d):
                ...

        is changed via this method to:

            def f(a, b=None, *c, **d):
                a = self.shadow(a)
                b = self.shadow(b)
                c = self.shadow(c)
                d = self.shadow(d)
                tag(a, 'a')
                tag(b, 'b')
                for k, v in d.items():
                    tag(v, k)
                ...

        This way, any future references to these variables will access their
        shadowed values. This is important because inplace modifications do
        not always force the `shadow` method to get called, and so the inplace
        changes might not be reflected the next (and first!) time the variable
        is loaded.
        """
        self.generic_visit(node)
        assigns = []
        tags = []

        # shadow and tag args
        for param in node.args.args:
            assigns.append(Assign(
                targets=[Name(ctx=Store(), id=param.id)],
                value=self.ast_wrap('shadow', Name(ctx=Load(), id=param.id))))

            tags.append(Expr(value=self.ast_wrap(
                method_name='tag_function_arg',
                args=[Name(ctx=Load(), id=param.id), Str(s=param.id)])))

        # shadow the varargs
        if node.args.vararg:
            assigns.append(Assign(
                targets=[Name(ctx=Store(), id=node.args.vararg)],
                value=self.ast_wrap('shadow', Name(ctx=Load(),
                                                   id=node.args.vararg))))

        # shadow and tag the kwargs
        if node.args.kwarg:
            assigns.append(Assign(
                targets=[Name(ctx=Store(), id=node.args.kwarg)],
                value=self.ast_wrap('shadow', Name(ctx=Load(),
                                                   id=node.args.kwarg))))

            tags.append(For(
                body=[Expr(value=self.ast_wrap(
                    method_name='tag_function_arg',
                    args=[Name(ctx=Load(), id='v'),
                          Name(ctx=Load(), id='k')]))],
                iter=simple_Call(
                    func=Attribute(attr='iteritems',
                                   ctx=Load(),
                                   value=Name(ctx=Load(),
                                              id=node.args.kwarg))),
                orelse=[],
                target=Tuple(ctx=Store(), elts=[Name(ctx=Store(), id='k'),
                                                Name(ctx=Store(), id='v')])))

        if node is self.context._top_def:
            node.body = assigns + tags + node.body
            self.context._top_def = None
        else:
            node.body = assigns + node.body

        return node

    def visit_If(self, node):
        """
        Transform this:

            if <statement>:
                ...
            else:
                ...

        to this:

            if escape(<statement>):
                ...
            else:
                ...

        This means that the if statement's test clause will be evaluated at
        runtime. Note that this does NOT carry over to the compiled Theano code.
        It just protects against the following case:

            if x:
                <do something>

        If x is a shadowed variable, then it always resolves to True. However,
        x could have a value of 0, in which case this shouldn't pass. Escaping
        x resolves it when the function is called.
        """
        self.generic_visit(node)
        node.test = self.ast_wrap('escape', node.test)
        return node