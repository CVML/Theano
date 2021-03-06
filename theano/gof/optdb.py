from __future__ import print_function
import sys

import numpy

from theano.compat import DefaultOrderedDict
from theano.misc.ordered_set import OrderedSet
from theano.compat.six import StringIO
from theano.gof import opt
from theano.configparser import AddConfigVar, FloatParam
from theano import config

AddConfigVar('optdb.position_cutoff',
             'Where to stop eariler during optimization. It represent the'
             ' position of the optimizer where to stop.',
             FloatParam(numpy.inf),
             in_c_key=False)
AddConfigVar('optdb.max_use_ratio',
             'A ratio that prevent infinite loop in EquilibriumOptimizer.',
             FloatParam(5),
             in_c_key=False)


class DB(object):
    def __hash__(self):
        if not hasattr(self, '_optimizer_idx'):
            self._optimizer_idx = opt._optimizer_idx[0]
            opt._optimizer_idx[0] += 1
        return self._optimizer_idx

    def __init__(self):
        self.__db__ = DefaultOrderedDict(OrderedSet)
        self._names = set()
        self.name = None  # will be reset by register
        # (via obj.name by the thing doing the registering)

    def register(self, name, obj, *tags, **kwargs):
        """
        :param name: name of the optimizer.
        :param obj: the optimizer to register.
        :param tags: tag name that allow to select the optimizer.
        :param kwargs: If non empty, should contain
            only use_db_name_as_tag=False.
            By default, all optimizations registered in EquilibriumDB
            are selected when the EquilibriumDB name is used as a
            tag. We do not want this behavior for some optimizer like
            local_remove_all_assert. use_db_name_as_tag=False remove
            that behavior. This mean only the optimizer name and the
            tags specified will enable that optimization.

        """
        # N.B. obj is not an instance of class Optimizer.
        # It is an instance of a DB.In the tests for example,
        # this is not always the case.
        if not isinstance(obj, (DB, opt.Optimizer, opt.LocalOptimizer)):
            raise TypeError('Object cannot be registered in OptDB', obj)
        if name in self.__db__:
            raise ValueError('The name of the object cannot be an existing'
                             ' tag or the name of another existing object.',
                             obj, name)
        if kwargs:
            assert "use_db_name_as_tag" in kwargs
            assert kwargs["use_db_name_as_tag"] is False
        else:
            if self.name is not None:
                tags = tags + (self.name,)
        obj.name = name
        # This restriction is there because in many place we suppose that
        # something in the DB is there only once.
        if obj.name in self.__db__:
            raise ValueError('''You can\'t register the same optimization
multiple time in a DB. Tryed to register "%s" again under the new name "%s".
 Use theano.gof.ProxyDB to work around that''' % (obj.name, name))
        self.__db__[name] = OrderedSet([obj])
        self._names.add(name)
        self.__db__[obj.__class__.__name__].add(obj)
        self.add_tags(name, *tags)

    def add_tags(self, name, *tags):
        obj = self.__db__[name]
        assert len(obj) == 1
        obj = obj.copy().pop()
        for tag in tags:
            if tag in self._names:
                raise ValueError('The tag of the object collides with a name.',
                                 obj, tag)
            self.__db__[tag].add(obj)

    def remove_tags(self, name, *tags):
        obj = self.__db__[name]
        assert len(obj) == 1
        obj = obj.copy().pop()
        for tag in tags:
            if tag in self._names:
                raise ValueError('The tag of the object collides with a name.',
                                 obj, tag)
            self.__db__[tag].remove(obj)

    def __query__(self, q):
        if not isinstance(q, Query):
            raise TypeError('Expected a Query.', q)
        # The ordered set is needed for deterministic optimization.
        variables = OrderedSet()
        for tag in q.include:
            variables.update(self.__db__[tag])
        for tag in q.require:
            variables.intersection_update(self.__db__[tag])
        for tag in q.exclude:
            variables.difference_update(self.__db__[tag])
        remove = OrderedSet()
        add = OrderedSet()
        for obj in variables:
            if isinstance(obj, DB):
                sq = q.subquery.get(obj.name, q)
                if sq:
                    replacement = obj.query(sq)
                    replacement.name = obj.name
                    remove.add(obj)
                    add.add(replacement)
        variables.difference_update(remove)
        variables.update(add)
        return variables

    def query(self, *tags, **kwtags):
        if len(tags) >= 1 and isinstance(tags[0], Query):
            if len(tags) > 1 or kwtags:
                raise TypeError('If the first argument to query is a Query,'
                                ' there should be no other arguments.',
                                tags, kwtags)
            return self.__query__(tags[0])
        include = [tag[1:] for tag in tags if tag.startswith('+')]
        require = [tag[1:] for tag in tags if tag.startswith('&')]
        exclude = [tag[1:] for tag in tags if tag.startswith('-')]
        if len(include) + len(require) + len(exclude) < len(tags):
            raise ValueError("All tags must start with one of the following"
                             " characters: '+', '&' or '-'", tags)
        return self.__query__(Query(include=include,
                                    require=require,
                                    exclude=exclude,
                                    subquery=kwtags))

    def __getitem__(self, name):
        variables = self.__db__[name]
        if not variables:
            raise KeyError("Nothing registered for '%s'" % name)
        elif len(variables) > 1:
            raise ValueError('More than one match for %s (please use query)' %
                             name)
        for variable in variables:
            return variable

    def print_summary(self, stream=sys.stdout):
        print("%s (id %i)" % (self.__class__.__name__, id(self)), file=stream)
        print("  names", self._names, file=stream)
        print("  db", self.__db__, file=stream)


class Query(object):

    def __init__(self, include, require=None, exclude=None,
                 subquery=None, position_cutoff=None):
        """
        :type position_cutoff: float
        :param position_cutoff: Used by SequenceDB to keep only optimizer that
                                are positioned before the cut_off point.
        """
        self.include = OrderedSet(include)
        self.require = require or OrderedSet()
        self.exclude = exclude or OrderedSet()
        self.subquery = subquery or {}
        self.position_cutoff = position_cutoff
        if isinstance(self.require, (list, tuple)):
            self.require = OrderedSet(self.require)
        if isinstance(self.exclude, (list, tuple)):
            self.exclude = OrderedSet(self.exclude)

    def __str__(self):
        return ("Query{inc=%s,ex=%s,require=%s,subquery=%s,"
                "position_cutoff=%d}" %
                (self.include, self.exclude, self.require, self.subquery,
                 self.position_cutoff))

    # add all opt with this tag
    def including(self, *tags):
        return Query(self.include.union(tags),
                     self.require,
                     self.exclude,
                     self.subquery,
                     self.position_cutoff)

    # remove all opt with this tag
    def excluding(self, *tags):
        return Query(self.include,
                     self.require,
                     self.exclude.union(tags),
                     self.subquery,
                     self.position_cutoff)

    # keep only opt with this tag.
    def requiring(self, *tags):
        return Query(self.include,
                     self.require.union(tags),
                     self.exclude,
                     self.subquery,
                     self.position_cutoff)


class EquilibriumDB(DB):
    """A set of potential optimizations which should be applied in an
        arbitrary order until equilibrium is reached.

    Canonicalize, Stabilize, and Specialize are all equilibrium optimizations.

    :param ignore_newtrees: If False, we will apply local opt on new
        node introduced during local optimization application. This
        could result in less fgraph iterations, but this don't mean it
        will be faster globally.

    .. note::

        We can put LocalOptimizer and Optimizer as EquilibriumOptimizer
        suppor both.

    """
    def __init__(self, ignore_newtrees=True):
        super(EquilibriumDB, self).__init__()
        self.ignore_newtrees = ignore_newtrees
        self.__final__ = {}

    def register(self, name, obj, *tags, **kwtags):
        # if name == 'cut_gpua_constant_transfers':
        #     import ipdb;ipdb.set_trace()
        if 'final_opt' in kwtags:
            final_opt = kwtags['final_opt']
            kwtags.pop('final_opt', None)
        else:
            final_opt = False
        super(EquilibriumDB, self).register(name, obj, *tags, **kwtags)
        self.__final__[name] = final_opt

    def query(self, *tags, **kwtags):
        _opts = super(EquilibriumDB, self).query(*tags, **kwtags)
        final_opts = [o for o in _opts if self.__final__.get(o.name, False)]
        opts = [o for o in _opts if o not in final_opts]
        if len(final_opts) == 0:
            final_opts = None
        return opt.EquilibriumOptimizer(
            opts,
            max_use_ratio=config.optdb.max_use_ratio,
            ignore_newtrees=self.ignore_newtrees,
            failure_callback=opt.NavigatorOptimizer.warn_inplace,
            final_optimizers=final_opts)


class SequenceDB(DB):
    """A sequence of potential optimizations.

    Retrieve a sequence of optimizations (a SeqOptimizer) by calling query().

    Each potential optimization is registered with a floating-point position.
    No matter which optimizations are selected by a query, they are carried
    out in order of increasing position.

    The optdb itself (`theano.compile.mode.optdb`), from which (among many
    other tags) fast_run and fast_compile optimizers are drawn is a SequenceDB.

    """
    seq_opt = opt.SeqOptimizer

    def __init__(self, failure_callback=opt.SeqOptimizer.warn):
        super(SequenceDB, self).__init__()
        self.__position__ = {}
        self.failure_callback = failure_callback

    def register(self, name, obj, position, *tags):
        super(SequenceDB, self).register(name, obj, *tags)
        self.__position__[name] = position

    def query(self, *tags, **kwtags):
        """
        :type position_cutoff: float or int
        :param position_cutoff: only optimizations with position less than
                                the cutoff are returned.
        """
        opts = super(SequenceDB, self).query(*tags, **kwtags)

        position_cutoff = kwtags.pop('position_cutoff',
                                     config.optdb.position_cutoff)
        if len(tags) >= 1 and isinstance(tags[0], Query):
            # the call to super should have raise an error with a good message
            assert len(tags) == 1
            if getattr(tags[0], 'position_cutoff', None):
                position_cutoff = tags[0].position_cutoff

        opts = [o for o in opts if self.__position__[o.name] < position_cutoff]
        # We want to sort by position and then if collision by name
        # for deterministic optimization.  Since Python 2.2, sort is
        # stable, so sort by name first, then by position. This give
        # the order we want.
        opts.sort(key=lambda obj: obj.name)
        opts.sort(key=lambda obj: self.__position__[obj.name])
        kwargs = {}
        if self.failure_callback:
            kwargs["failure_callback"] = self.failure_callback
        ret = self.seq_opt(opts, **kwargs)
        if hasattr(tags[0], 'name'):
            ret.name = tags[0].name
        return ret

    def print_summary(self, stream=sys.stdout):
        print(self.__class__.__name__ + " (id %i)" % id(self), file=stream)
        positions = self.__position__.items()

        def c(a, b):
            return cmp(a[1], b[1])
        positions.sort(c)

        print("  position", positions, file=stream)
        print("  names", self._names, file=stream)
        print("  db", self.__db__, file=stream)

    def __str__(self):
        sio = StringIO()
        self.print_summary(sio)
        return sio.getvalue()


class LocalGroupDB(SequenceDB):
    """This generate a local optimizer of type LocalOptGroup instead
    of a global optimizer.

    It support the tracks, to only get applied to some Op.
    """
    seq_opt = opt.LocalOptGroup

    def __init__(self, failure_callback=opt.SeqOptimizer.warn):
        super(LocalGroupDB, self).__init__()
        self.failure_callback = None


class ProxyDB(DB):
    """
    Wrap an existing proxy.

    This is needed as we can't register the same DB mutiple time in
    different position in a SequentialDB
    """
    def __init__(self, db):
        assert isinstance(db, DB), ""
        self.db = db

    def query(self, *tags, **kwtags):
        return self.db.query(*tags, **kwtags)
