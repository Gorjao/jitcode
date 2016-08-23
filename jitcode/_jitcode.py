#!/usr/bin/python3
# -*- coding: utf-8 -*-

from __future__ import print_function, absolute_import

from scipy.integrate import ode
from os import path as path
from sys import version_info, modules
from numpy import array, hstack, log
from warnings import warn
from traceback import format_exc
from types import FunctionType, BuiltinFunctionType
from setuptools import setup, Extension
from tempfile import mkdtemp
from inspect import getargspec, isgeneratorfunction
from scipy.integrate._ode import find_integrator
from copy import copy as copy_object
from itertools import chain, count
from jitcode._helpers import (
	ensure_suffix, count_up,
	get_module_path, modulename_from_path, find_and_load_module, module_from_path,
	render_and_write_code, render_and_write_code_old,
	render_template,
	non_zero_ratio, random_direction, orthonormalise
	)
import sympy
import shutil

def provide_basic_symbols():
	"""
	provides the basic symbols that must be used to define the differential equation. You may just as well define the respective symbols and functions directly with SymPy, but using this function is the best way to get the most of future versions of JiTCODE, in particular avoiding incompatibilities. If you wish to use other symbols for the dynamical variables, you can use `convert_to_required_symbols` for conversion.
	
	Returns
	-------
	t : SymPy symbol
		represents time
	y : SymPy function
		represents the ODE’s state, with the integer argument denoting the component
	"""
	
	return sympy.Symbol("t", real=True), sympy.Function("y")

def convert_to_required_symbols(dynvars, f_sym, helpers=[], n=None):
	"""
	This is a service function to convert a differential equation defined using other symbols for the dynamical variables to the format required by JiTCODE.
	
	Parameters
	----------
	
	dynvars : iterable of SymPy expressions.
		The dynamical variables used to define the differential equation in `f_sym`. These must be in the same order as `f_sym` gives their derivatives.
	f_sym : iterable of SymPy expressions or generator function yielding SymPy expressions
		same as the respective input for `jitcode` apart from using `dynvars` as dynamical variables
	helpers : list of length-two iterables, each containing a SymPy symbol and a SymPy expression
		same as the respective input for `jitcode`
	n : integer
		same as the respective input for `jitcode`
	
	Returns
	-------
	argument_dictionary : dictionary
		arguments that are fit for being passed to `jitcode`, e.g. like this: `jitcode(wants_jacobian=True, **argument_dictionary)`. This contains the arguments `f_sym`, `helpers`, and `n`.
	"""
	
	f_sym, n = _handle_input(f_sym, n)
	
	_, y = provide_basic_symbols()
	substitutions = [(dynvar, y(i)) for i,dynvar in enumerate(dynvars)]
	
	def f():
		for entry in f_sym():
			yield entry.subs(substitutions)
	helpers = [(helper[0], helper[1].subs(substitutions)) for helper in helpers]
	return {"f_sym": f, "helpers": helpers, "n": n}

def ode_from_module_file(location):
	"""
	loads functions from a module file generated by JiTCODE (see `save_compiled`).

	Parameters
	----------
	location : string
		location of the module file to be loaded.

	Returns
	-------
	instance of `scipy.integrate.ode`
		This is initiated with the functions found in the module file. Note that this is **not** an instance of `jitcode`.
	"""

	module = module_from_path(location)

	if hasattr(module,"jac"):
		return ode(module.f,module.jac)
	else:
		return ode(module.f)

def _can_use_jacobian(integratorname):
	integrator = find_integrator(integratorname)
	argspec = getargspec(integrator.__init__)
	return "with_jacobian" in argspec.args

def _is_C(function):
	return isinstance(function, BuiltinFunctionType)

def _is_lambda(function):
	return isinstance(function, FunctionType)

def _sympify_helpers(helpers):
	return [(helper[0], sympy.sympify(helper[1]).doit()) for helper in helpers]
	#return [tuple(map(sympy.sympify, helper)) for helper in helpers]


def depends_on_any(helper, other_helpers):
	for other_helper in other_helpers:
		if helper[1].has(other_helper[0]):
			return True
	return False

def _sort_helpers(helpers):
	if len(helpers)>1:
		for j,helper in enumerate(helpers):
			if not depends_on_any(helper, helpers):
				helpers.insert(0,helpers.pop(j))
				break
		else:
			raise ValueError("Helpers have cyclic dependencies.")
		
		helpers[1:] = _sort_helpers(helpers[1:])
	
	return helpers

def _jac_from_f_with_helpers(f, helpers, simplify, n):
	t,y = provide_basic_symbols()
	
	dependent_helpers = [[] for i in range(n)]
	for i in range(n):
		for helper in helpers:
			derivative = 0
			if helper[1].has(y(i)):
				derivative = sympy.diff(helper[1], y(i))
			for other_helper in dependent_helpers[i]:
				if helper[1].has(other_helper[0]):
					derivative += sympy.diff(helper[1],other_helper[0]) * other_helper[1]
			if derivative:
				dependent_helpers[i].append( (helper[0], derivative) )
	
	def line(f_entry):
		for j in range(n):
			entry = sympy.diff( f_entry, y(j) )
			for helper in dependent_helpers[j]:
				entry += sympy.diff(f_entry,helper[0]) * helper[1]
			if simplify:
				entry = sympy.simplify(entry, ratio=1.0)
			yield entry
	
	for f_entry in f():
		yield line(f_entry)

def _handle_input(f_sym,n):
	if isgeneratorfunction(f_sym):
		n = n or sum(1 for _ in f_sym())
		return ( f_sym, n )
	else:
		len_f = len(f_sym)
		if (n is not None) and(len_f != n):
			raise ValueError("len(f_sym) and n do not match.")
		return (lambda: (entry.doit() for entry in f_sym), len_f)

#: A list with the default extra compile arguments. Use and modify these to get the most of future versions of JiTCODE. Note that without `-Ofast`, `-ffast-math`, or `-funsafe-math-optimizations` (if supported by your compiler), you may experience a considerable speed loss since SymPy uses the `pow` function for small integer powers (`SymPy Issue 8997`_).
DEFAULT_COMPILE_ARGS = [
			"-Ofast",
			"-g0",
			"-march=native",
			"-mtune=native",
			"-Wno-unknown-pragmas",
			]

class jitcode(ode):
	"""
	Parameters
	----------
	f_sym : iterable of SymPy expressions or generator function yielding SymPy expressions
		The `i`-th element is the `i`-th component of the value of the ODE’s derivative :math:`f(t,y)`.
	
	helpers : list of length-two iterables, each containing a SymPy symbol and a SymPy expression
		Each helper is a variable that will be calculated before evaluating the derivative and can be used in the latter’s computation. The first component of the tuple is the helper’s symbol as referenced in the derivative or other helpers, the second component describes how to compute it from `t`, `y` and other helpers. This is for example useful to realise a mean-field coupling, where the helper could look like `(mean, sympy.Sum(y(i),(i,0,99))/100)`. (See `example_2` for an example.)
	
	wants_jacobian : boolean
		Tell JiTCODE to calculate and compile the Jacobian. For vanilla use, you do not need to bother about this as this is automatically set to `True` if the selected method of integration desires the Jacobian. However, it is sometimes useful if you want to manually apply some code-generation steps (e.g., to apply some tweaks).
		
	n : integer
		Length of `f_sym`. While JiTCODE can easily determine this itself (and will, if necessary), this may take some time if `f_sym` is a generator function and `n` is large. Take care that this value is correct – if it isn’t, you will not get a helpful error message.
	
	silent : boolean
		Whether JiTCODE shall give progress reports on the processing steps.
	"""
	
	# Naming convention:
	# If an underscore-prefixed and regular variant of a function exist, the ormer calls the latter if needed and tells the user what it did.
	
	def __init__(self, f_sym, helpers=None, wants_jacobian=False, n=None, verbose=True):
		self.f_sym, self.n = _handle_input(f_sym,n)
		self.f = None
		self._f_C_source = False
		self.helpers = _sort_helpers(_sympify_helpers(helpers or []))
		self._wants_jacobian = wants_jacobian
		self.jac_sym = None
		self.jac = None
		self._jac_C_source = False
		self._y = []
		self._tmpdir = None
		self._modulename = "jitced"
		self.verbose = verbose
		self._number_of_jac_helpers = None
		self._number_of_f_helpers = None
	
	def _tmpfile(self, filename=None):
		if self._tmpdir is None:
			self._tmpdir = mkdtemp()
		
		if filename is None:
			return self._tmpdir
		else:
			return path.join(self._tmpdir, filename)
	
	def report(self, message):
		if self.verbose:
			print(message)
	
	def _generate_jac_sym(self):
		if self.jac_sym is None:
			self.generate_jac_sym()
			#self.report("generated symbolic Jacobian")
	
	def generate_jac_sym(self, simplify=True):
		"""
		generates the Jacobian using SymPy’s differentiation.
		
		Parameters
		----------
		simplify : boolean
			Whether the resulting Jacobian should be `simplified <http://docs.sympy.org/dev/modules/simplify/simplify.html>`_ (with `ratio=1.0`). This is almost always a good thing.
		"""
		
		self.jac_sym = _jac_from_f_with_helpers(self.f_sym, self.helpers, simplify, self.n)
	
	def _generate_f_C(self):
		if not self._f_C_source:
			self.generate_f_C()
			self.report("generated C code for f")
	
	def generate_f_C(self, simplify=True, do_cse=False, chunk_size=100):
		"""
		translates the derivative to C code using SymPy’s `C-code printer <http://docs.sympy.org/dev/modules/printing.html#module-sympy.printing.ccode>`_.
		
		Parameters
		----------
		simplify : boolean
			Whether the derivative should be `simplified <http://docs.sympy.org/dev/modules/simplify/simplify.html>`_ (with `ratio=1.0`) before translating to C code. The main reason why you could want to disable this is if your derivative is already  optimised and so large that simplifying takes a considerable amount of time.
		
		do_cse : boolean
			Whether SymPy’s `common-subexpression detection <http://docs.sympy.org/dev/modules/rewriting.html#module-sympy.simplify.cse_main>`_ should be applied before translating to C code. It is almost always better to let the compiler do this (unless you want to set the compiler optimisation to `-O2` or lower): For simple differential equations this should not make any difference to the compiler’s optimisations. For large ones, it may make a difference but also take long. As this requires all entries of `f` at once, it may void advantages gained from using generator functions as an input.
		
		chunk_size : integer
			If the number of instructions in the final C code exceeds this number, it will be split into chunks of this size. After the generation of each chunk, SymPy’s cache is cleared. See `large_systems` on why this is useful.
			
			If there is an obvious grouping of your :math:`f`, the group size suggests itself for `chunk_size`. For example, if you want to simulate the dynamics of three-dimensional oscillators coupled onto a 40×40 lattice and if the differential equations are grouped first by oscillator and then by lattice row, a chunk size of 120 suggests itself.
			
			If smaller than 1, no chunking will happen.
		"""
		
		f_sym_wc = self.f_sym()
		
		if simplify:
			f_sym_wc = (sympy.simplify(entry,ratio=1) for entry in f_sym_wc)
		
		arguments = [("dY", "PyArrayObject*"),("Y", "PyArrayObject*")]
		
		if do_cse:
			get_helper = sympy.Function("get_f_helper")
			set_helper = sympy.Function("set_f_helper")
			
			_cse = sympy.cse(
					sympy.Matrix(list(self.f_sym())),
					symbols = (get_helper(i) for i in count())
				)
			more_helpers = _cse[0]
			f_sym_wc = _cse[1][0]
			
			if more_helpers:
				render_and_write_code(
					(set_helper(i, helper[1]) for i,helper in enumerate(more_helpers)),
					self._tmpfile,
					"f_helpers",
					{"y":"y", "get_f_helper":"get_f_helper", "set_f_helper":"set_f_helper"},
					chunk_size = chunk_size,
					arguments = [("Y", "PyArrayObject*"), ("f_helper","double*")]
					)
				self._number_of_f_helpers = len(more_helpers)
				arguments.append(("f_helper","double*"))
		
		set_dy = sympy.Function("set_dy")
		render_and_write_code(
			(set_dy(i,entry) for i,entry in enumerate(f_sym_wc)),
			self._tmpfile,
			"f",
			{"set_dy":"set_dy", "y":"y", "get_f_helper":"get_f_helper"},
			chunk_size = chunk_size,
			arguments = arguments
			)
		
		self._f_C_source = True
	
	def _generate_jac_C(self):
		if self._wants_jacobian and not self._jac_C_source:
			self.generate_jac_C()
			self.report("generated C code for Jacobian")
	
	def generate_jac_C(self, do_cse=False, chunk_size=100, sparse=True):
		"""
		translates the symbolic Jacobian to C code using SymPy’s `C-code printer <http://docs.sympy.org/dev/modules/printing.html#module-sympy.printing.ccode>`_. If the symbolic Jacobian has not been generated, it generates it by calling `generate_jac_sym`.
		
		Parameters
		----------
		
		do_cse : boolean
			Whether SymPy’s `common-subexpression detection <http://docs.sympy.org/dev/modules/rewriting.html#module-sympy.simplify.cse_main>`_ should be applied before translating to C code. It is almost always better to let the compiler do this (unless you want to set the compiler optimisation to `-O2` or lower): For simple differential equations this should not make any difference to the compiler’s optimisations. For large ones, it may make a difference but also take long. As this requires the entire Jacobian at once, it may void advantages gained from using generator functions as an input.
			
		chunk_size : integer
			If the number of instructions in the final C code exceeds this number, it will be split into chunks of this size. After the generation of each chunk, SymPy’s cache is cleared. See `large_systems` on why this is useful.
			
			If there is an obvious grouping of your Jacobian, the respective group size suggests itself for `chunk_size`. For example, the derivative of each dynamical variable explicitly depends on 60 others and the Jacobian is sparse, a chunk size of 60 suggests itself.
			
			If smaller than 1, no chunking will happen.
		
		sparse : boolean
			Whether a sparse Jacobian should be assumed for optimisation. Note that this does not mean that the Jacobian is stored, parsed or handled as a sparse matrix. This kind of optimisation would require SciPy’s ODE to be able to handle sparse matrices.
		"""
		
		self._generate_jac_sym()
		jac_sym_wc = self.jac_sym
		self.sparse_jac = sparse
		
		arguments = [("dfdY", "PyArrayObject*"), ("Y", "PyArrayObject*")]
		
		if do_cse:
			jac_matrix = sympy.Matrix([ [entry for entry in line] for line in jac_sym_wc ])
			
			get_helper = sympy.Function("get_jac_helper")
			set_helper = sympy.Function("set_jac_helper")
			
			_cse = sympy.cse(
					jac_matrix,
					symbols = (get_helper(i) for i in count())
				)
			more_helpers = _cse[0]
			jac_sym_wc = _cse[1][0].tolist()
			
			if more_helpers:
				render_and_write_code(
					(set_helper(i, helper[1]) for i,helper in enumerate(more_helpers)),
					self._tmpfile,
					"jac_helpers",
					{"y":"y", "get_jac_helper":"get_jac_helper", "set_jac_helper":"set_jac_helper"},
					chunk_size = chunk_size,
					arguments = [("Y", "PyArrayObject*"), ("jac_helper","double*")]
					)
				self._number_of_jac_helpers = len(more_helpers)
				arguments.append(("jac_helper","double*"))
		
		set_dfdy = sympy.Function("set_dfdy")
		
		render_and_write_code(
			(
				set_dfdy(i,j,entry)
				for i,line in enumerate(jac_sym_wc)
				for j,entry in enumerate(line)
				if ( (entry != 0) or not self.sparse_jac )
			),
			self._tmpfile,
			"jac",
			{"set_dfdy":"set_dfdy", "y":"y", "get_jac_helper":"get_jac_helper"},
			chunk_size = chunk_size,
			arguments = arguments
		)

		
		self._jac_C_source = True
	
	def _generate_helpers_C(self):
		if self.helpers:
			self.generate_helpers_C()
			self.report("generated C code for helpers")
	
	def generate_helpers_C(self, chunk_size=100):
		"""
		translates the helpers to C code using SymPy’s `C-code printer <http://docs.sympy.org/dev/modules/printing.html#module-sympy.printing.ccode>`_.
		
		Parameters
		----------
		chunk_size : integer
			If the number of instructions in the final C code exceeds this number, it will be split into chunks of this size. After the generation of each chunk, SymPy’s cache is cleared. See `large_systems` on why this is useful.
			
			If there is an obvious grouping of your helpers, the group size suggests itself for `chunk_size`.
			
			If smaller than 1, no chunking will happen.
		"""
		
		render_and_write_code_old(
			[],
			self.helpers,
			self._tmpfile,
			"general",
			{"y":"y"},
			chunk_size = chunk_size
			)
	
	def _compile_C(self):
		if (not _is_C(self.f)) or (self._wants_jacobian and not _is_C(self.jac)):
			self.compile_C()
			self.report("compiled C code")
	
	def compile_C(
		self,
		extra_compile_args = DEFAULT_COMPILE_ARGS,
		verbose = False,
		modulename = None
		):
		"""
		compiles the C code (using `Setuptools <http://pythonhosted.org/setuptools/>`_) and loads the compiled functions. If no C code exists, it is generated by calling `generate_f_C` and `generate_jac_C`.
		
		Parameters
		----------
		extra_compile_args : list of strings
			Arguments to be handed to the C compiler on top of what Setuptools chooses. In most situations, it’s best not to write your own list, but modify `DEFAULT_COMPILE_ARGS`, e.g., like this: `compile_C(extra_compile_args = DEFAULT_COMPILE_ARGS + ["--my-flag"])`.
		verbose : boolean
			Whether the compiler commands shall be shown. This is the same as Setuptools’ `verbose` setting.
		modulename : string or `None`
			The name used for the compiled module. If `None` or empty, the filename will be chosen by JiTCODE based on previously used filenames or default to `jitced.so`. The only reason why you may want to change this is if you want to save the module file for later use (with`save_compiled`). It is not possible to re-use a modulename for a given instance of `jitcode` (due to the limitations of Python’s import machinery).
		
		Notes
		-----
		If you want to change the compiler, the intended way is your operating system’s `CC` flag, e.g., by calling `export CC=clang` in the terminal or `os.environ["CC"] = "clang"` in Python.
		"""
		
		self._generate_f_C()
		self._generate_jac_C()
		self._generate_helpers_C()
		
		if modulename:
			if modulename in modules.keys():
				raise NameError("Module name has already been used in this instance of Python.")
			self._modulename = modulename
		else:
			while self._modulename in modules.keys():
				self._modulename = count_up(self._modulename)
		
		sourcefile = self._tmpfile(self._modulename + ".c")
		modulefile = self._tmpfile(self._modulename + ".so")
		
		if path.isfile(modulefile):
			raise OSError("Module file already exists.")
		
		render_template(
			"jitced_template.c",
			sourcefile,
			n = self.n,
			has_Jacobian = self._jac_C_source,
			module_name = self._modulename,
			Python_version = version_info[0],
			has_helpers = bool(self.helpers),
			number_of_f_helpers = self._number_of_f_helpers or 0,
			number_of_jac_helpers = self._number_of_jac_helpers or 0,
			sparse_jac = self.sparse_jac if self._jac_C_source else None
			)
		
		setup(
			name = self._modulename,
			ext_modules = [Extension(
				self._modulename,
				sources = [sourcefile],
				extra_compile_args = extra_compile_args
				)],
			script_args = [
				"build_ext",
				"--build-lib", self._tmpfile(),
				"--build-temp", self._tmpfile(),
				"--force",
				#"clean" #, "--all"
				],
			verbose = verbose
			)
		
		self._jitced = find_and_load_module(self._modulename,self._tmpfile())
		
		self.f = self._jitced.f
		if self._jac_C_source:
			self.jac = self._jitced.jac
	
	def _generate_f_lambda(self):
		if not _is_lambda(self.f):
			self.generate_f_lambda()
			self.report("generated lambdified f")
	
	def generate_f_lambda(self, simplify=True):
		"""
		translates the symbolic derivative to a function using SymPy’s `lambdify <http://docs.sympy.org/latest/modules/utilities/lambdify.html>`_ tool.
		
		Parameters
		----------
		simplify : boolean
			Whether the derivative should be `simplified <http://docs.sympy.org/dev/modules/simplify/simplify.html>`_ (with `ratio=1.0`) before translating to C code. The main reason why you could want to disable this is if your derivative is already optimised and so large that simplifying takes a considerable amount of time.
		"""
		
		if self.helpers:
			warn("Lambdification does not handle helpers in an efficient manner.")
		
		t,y = provide_basic_symbols()
		Y = sympy.symarray("Y", self.n)
		
		substitutions = self.helpers[::-1] + [(y(i),Y[i]) for i in range(self.n)]
		f_sym_wc = (entry.subs(substitutions) for entry in self.f_sym())
		if simplify:
			f_sym_wc = (entry.simplify(ratio=1.0) for entry in f_sym_wc)
		F = sympy.lambdify([t]+[Yentry for Yentry in Y], list(f_sym_wc))
		
		self.f = lambda t,ypsilon: array(F(t,*ypsilon)).flatten()
	
	def _generate_jac_lambda(self):
		if not _is_lambda(self.jac):
			self.generate_jac_lambda()
			self.report("generated lambdified Jacobian")
	
	def generate_jac_lambda(self):
		"""
		translates the symbolic Jacobian to a function using SymPy’s `lambdify <http://docs.sympy.org/latest/modules/utilities/lambdify.html>`_ tool. If the symbolic Jacobian has not been generated, it is generated by calling `generate_jac_sym`.
		"""
		
		if self.helpers:
			warn("Lambdification handles helpers by pluggin them in. This may be very ineficient")
		
		self._generate_jac_sym()
		
		jac_matrix = sympy.Matrix([ [entry for entry in line] for line in self.jac_sym ])
		
		t,y = provide_basic_symbols()
		Y = sympy.symarray("Y", self.n)
		
		substitutions = self.helpers[::-1] + [(y(i),Y[i]) for i in range(self.n)]
		jac_subsed = jac_matrix.subs(substitutions)
		JAC = sympy.lambdify([t]+[Yentry for Yentry in Y], jac_subsed)
		
		self.jac = lambda t,ypsilon: array(JAC(t,*ypsilon))

	def generate_lambdas(self):
		"""
		If they do not already exists, this generates lambdified functions by calling `self.generate_f_lambda()` and, if wanted, `generate_jac_lambda()`.
		"""
		
		self._generate_f_lambda()
		if self._wants_jacobian:
			self._generate_jac_lambda()
	
	def _generate_functions(self):
		if (self.f is None) or (self._wants_jacobian and (self.jac is None)):
			self.generate_functions()
	
	def generate_functions(self):
		"""
		The central function-generating function. Tries to compile the derivative and, if wanted, the Jacobian. If this fails, it generates lambdified functions as a fallback.
		"""
		
		try:
			self._compile_C()
		except:
			warn(format_exc())
			
			warn("Generating compiled functions failed; resorting to lambdified functions.")
			self.generate_lambdas()
	
	def set_initial_value(self, y, t=0.0):
		"""
		Same as the analogous function in SciPy’s ODE. Note that if no integrator has been set yet, `set_integrator` will be called with all this implies, using an arbitrary integrator.
		"""
		
		if (self.n != len(y)):
			raise ValueError("The dimension of the initial value does not match the dimension of your differential equations.")
		
		if (not hasattr(self,"_integrator")) or (self._integrator is None):
			warn("No integrator set. Using first one available.")
		
		return super(jitcode, self).set_initial_value(y, t)
	
	def set_integrator(self, name, **integrator_params):
		"""
		Same as the analogous function in SciPy’s ODE, except that it automatically generates the derivative and Jacobian, if they do not exist yet and are needed.
		"""
		
		if name == 'zvode':
			raise NotImplementedError("JiTCODE does not natively support complex numbers yet.")
		
		self._wants_jacobian |= _can_use_jacobian(name)
		self._generate_functions()
		
		try:
			save_y = self._y
			save_t = self.t
		except AttributeError:
			super(jitcode, self).__init__(self.f, self.jac)
			super(jitcode, self).set_integrator(name, **integrator_params)
		else:
			super(jitcode, self).__init__(self.f, self.jac)
			super(jitcode, self).set_integrator(name, **integrator_params)
			super(jitcode, self).set_initial_value(save_y, save_t)
		
		return self

	def set_f_params(self, *args):
		raise NotImplementedError("JiTCODE does not support passing parameters to the derivative yet.")
	
	def set_jac_params(self, *args):
		raise NotImplementedError("JiTCODE does not support passing parameters to the Jacobian yet.")
	
	def save_compiled(self, destination="", overwrite=False):
		"""
		saves the module file with the compiled functions for later use (see `ode_from_module_file`). If no compiled derivative exists, it tries to compile it first using `compile_C`. In most circumstances, you should not rename this file, as the filename is needed to determine the module name.
		
		Parameters
		----------
		destination : string specifying a path
			If this specifies only a directory (don’t forget the trailing `/` or similar), the module will be saved to that directory. If empty (default), the module will be saved to the current working directory. Otherwise, the functions will be (re)compiled to match that filename. The ending `.so` will be appended, if needed.
		overwrite : boolean
			Whether to overwrite the specified target, if it already exists.
		"""
		
		folder, filename = path.split(destination)
		
		if filename:
			destination = ensure_suffix(destination, ".so")
			modulename = modulename_from_path(filename)
			if modulename != self._modulename:
				self.compile_C(modulename=modulename)
				self.report("compiled C code")
			sourcefile = get_module_path(self._modulename, self._tmpfile())
		else:
			self._compile_C()
			sourcefile = get_module_path(self._modulename, self._tmpfile())
			destination = path.join(folder, ensure_suffix(self._modulename, ".so"))
			self.report("saving file to " + destination)
		
		if path.isfile(destination) and not overwrite:
			raise OSError("Target File already exists and \"overwrite\" is set to False")
		else:
			shutil.copy(sourcefile, destination)
	
	def __del__(self):
		try:
			shutil.rmtree(self._tmpdir)
		except (OSError, AttributeError, TypeError):
			pass


class jitcode_lyap(jitcode):
	"""the handling is the same as that for `jitcode` except for:
	
	Parameters
	----------
	n_lyap : integer
		Number of Lyapunov exponents to calculate. If negative or larger than the dimension of the system, all Lyapunov exponents are calculated.
	"""
	
	def __init__(self, f_sym, helpers=None, wants_jacobian=False, n=None, n_lyap=-1):
		f_basic, n = _handle_input(f_sym,n)
		self.n_basic = n
		self._n_lyap = n if (n_lyap<0 or n_lyap>n) else n_lyap
		
		_,y = provide_basic_symbols()
		
		helpers = _sort_helpers(_sympify_helpers(helpers or []))
		
		def f_lyap():
			#Replace with yield from, once Python 2 is dead:
			for entry in f_basic():
				yield entry
			
			for i in range(self._n_lyap):
				for line in _jac_from_f_with_helpers(f_basic, helpers, False, n):
					yield sympy.simplify( sum( entry * y(k+(i+1)*n) for k,entry in enumerate(line) ), ratio=1.0 )
		
		super(jitcode_lyap, self).__init__(
			f_lyap,
			helpers = helpers,
			wants_jacobian = wants_jacobian,
			n = self._n_lyap*(n+1)
			)
	
	def set_initial_value(self, y, t=0.0):
		new_y = [y]
		for i in range(self._n_lyap):
			new_y += [random_direction(self.n_basic)]
		
		super(jitcode_lyap, self).set_initial_value(hstack(new_y), t)
	
	def integrate(self, *args, **kwargs):
		"""
		Like SciPy’s ODE’s `integrate`, except for orthonormalising the tangent vectors and:
		
		Returns
		-------
		y : one-dimensional NumPy array
			The first `len(f_sym)` entries are the state of the system.
			The remaining entries are the “local” Lyapunov exponents as estimated from the growth or shrinking of the tangent vectors during the integration time of this very `integrate` command, i.e., :math:`\\frac{\\ln (α_i^{(p)})}{s_i}` in the notation of [BGGS80]_
		"""
		
		old_t = self.t
		super(jitcode_lyap, self).integrate(*args, **kwargs)
		delta_t = self.t-old_t
		
		n = self.n_basic
		tangent_vectors = [ self._y[(i+1)*n:(i+2)*n] for i in range(self._n_lyap) ]
		norms = orthonormalise(tangent_vectors)
		lyaps = log(norms) / delta_t
		
		super(jitcode_lyap, self).set_initial_value(self._y, self.t)
		
		return hstack((self._y[:n], lyaps))
	
	def save_compiled(self, *args, **kwargs):
		warn("Your module will be saved, but note that there is no method to generate a jitcode_lyap instance from a saved module file yet.")
		super(jitcode_lyap, self).save_compiled(*args, **kwargs)

