from __future__ import print_function, division, with_statement
from jinja2 import Environment, FileSystemLoader
from sympy.printing.ccode import ccode
from sympy import Function
from sys import version_info, stderr
import numpy as np
from os import path
from warnings import warn
from itertools import chain, count
from sympy.core.cache import clear_cache


# String manipulation
# -------------------

def remove_suffix(string, suffix):
	partition = string.rpartition(suffix)
	if partition[1] and not partition[2]:
		return partition[0]
	else:
		return string

def ensure_suffix(string, suffix):
	if not string.endswith(suffix):
		return string + suffix
	else:
		return string

def rsplit_int(s):
	if s and s[-1].isdigit():
		x,y = rsplit_int(s[:-1])
		return x, y+s[-1]
	else:
		return s, ""

def count_up(name):
	s, i = rsplit_int(name)
	return s + ( "%%.%ii" % len(i) % (int(i)+1)  if i else "_1" )

# Module handling
# ---------------

if version_info < (3,):
	
	from imp import find_module, load_module, load_dynamic
	
	def modulename_from_path(full_path):
		filename = path.basename(full_path)
		return remove_suffix(filename, ".so")
	
	def get_module_path(modulename, folder=""):
		return find_module(modulename, [folder])[1]
	
	def find_and_load_module(modulename, folder=""):
		specs = find_module(modulename, [folder])
		return load_module(modulename, *specs)
	
	def module_from_path(full_path):
		return load_dynamic( modulename_from_path(full_path), full_path )
	
elif version_info >= (3,3):
	
	from importlib.machinery import ExtensionFileLoader, EXTENSION_SUFFIXES, FileFinder
	from importlib.util import spec_from_file_location
	
	loader_details = (ExtensionFileLoader, EXTENSION_SUFFIXES)
	
	def modulename_from_path(full_path):
		filename = path.basename(full_path)
		for suffix in sorted(EXTENSION_SUFFIXES, key=len, reverse=True):
			filename = remove_suffix(filename, suffix)
		return filename
	
	def get_module_path(modulename, folder=""):
		finder = FileFinder(folder, loader_details)
		return finder.find_spec(modulename).origin
	
	if version_info < (3,5):
		def find_and_load_module(modulename, folder=""):
			finder = FileFinder(folder, loader_details)
			spec = finder.find_spec(modulename)
			return spec.loader.load_module()
		
		def module_from_path(full_path):
			modulename = modulename_from_path(full_path)
			spec = spec_from_file_location(modulename, full_path)
			return spec.loader.load_module()
		
	else:
		warn("Import routines for Python 3.5 and higher are untested.")
		
		from importlib.util import module_from_spec
		
		def find_and_load_module(modulename, folder=""):
			finder = FileFinder(folder, loader_details)
			spec = finder.find_spec(modulename)
			module = module_from_spec(spec)
			spec.loader.exec_module(module)
			return module
		
		def module_from_path(full_path):
			modulename = modulename_from_path(full_path)
			spec = spec_from_file_location(modulename, full_path)
			module = module_from_spec(spec)
			spec.loader.exec_module(module)
			return module
	
else:
	
	raise NotImplementedError("Module loading for Python versions between 3 and 3.3 was not implemented. Please upgrade to a newer Python version.")

# Code and templates
# ------------------

def check_code(code):
	if code.startswith("// Not"):
		stderr.write(code)
		raise Exception("The above expression could not be converted to C Code.")
	return code

def render_declarations(expressions, filename):
	with open(filename, "w") as output:
		for expression in expressions:
			output.write("double " + ccode(expression) + ";\n")

def write_in_chunks(lines, mainfile, deffile, name, chunk_size, arguments):
	funcname = "definitions_" + name
	
	first_chunk = []
	try:
		for i in range(chunk_size+1):
			first_chunk.append(next(lines))
	except StopIteration:
		for line in first_chunk:
			mainfile.write(line)
	else:
		lines = chain(first_chunk, lines)
		
		while True:
			mainfile.write(funcname + "(")
			deffile.write("void " + funcname + "(")
			if arguments:
				mainfile.write(", ".join(argument[0] for argument in arguments))
				deffile.write(", ".join(argument[1]+" "+argument[0] for argument in arguments))
			else:
				deffile.write("void")
			mainfile.write(");\n")
			deffile.write("){\n")
			
			try:
				for i in range(chunk_size):
					deffile.write(next(lines))
			except StopIteration:
				break
			finally:
				deffile.write("}\n")
			
			funcname = count_up(funcname)
			clear_cache()

def render_and_write_code_old(
	expressions,
	helpers,
	tmpfile,
	name,
	user_functions = {},
	chunk_size = 100,
	arguments = []
	):
	
	render_declarations(
		(helper[0] for helper in helpers),
		tmpfile("declare_"+name+"_helpers.c")
	)
	
	helperlines = (
		check_code( ccode( helper[1], helper[0], user_functions=user_functions ) ) + "\n"
		for helper in helpers
		)
	codelines = (
		check_code( ccode ( expression, user_functions=user_functions ) ) + ";\n"
		for expression in expressions
		)
	
	with \
		open( tmpfile(name+".c"            ), "w" ) as mainfile, \
		open( tmpfile(name+"_definitions.c"), "w" ) as deffile:
		if chunk_size < 1:
			for line in chain(helperlines, codelines):
				mainfile.write(line)
		else:
			write_in_chunks(helperlines, mainfile, deffile, name+"helpers", chunk_size, arguments[1:])
			write_in_chunks(codelines  , mainfile, deffile, name+"code"   , chunk_size, arguments)

def render_and_write_code(
	expressions,
	helpers,
	tmpfile,
	name,
	user_functions = {},
	chunk_size = 100,
	arguments = []
	):
	
	set_helper = Function("set_"+name+"_helper")
	get_helper = Function("get_"+name+"_helper")
	substitutions = [(helper[0], get_helper(i)) for i,helper in enumerate(helpers)]
	
	user_functions["set_"+name+"_helper"] = "set_"+name+"_helper"
	user_functions["get_"+name+"_helper"] = "get_"+name+"_helper"
	
	with open(tmpfile("declare_"+name+"_helpers.c"), "w") as output:
		output.write("# define get_%s_helper(i) ((%s_helper[i]))\n"%(name,name))
		output.write("# define set_%s_helper(i,value) (%s_helper[i] = value)\n"%(name,name))
	
	def helperlines():
		for i,helper in enumerate(helpers):
			expression = set_helper(i, helper[1].subs(substitutions))
			codeline = ccode(expression, user_functions=user_functions)
			yield check_code(codeline) + ";\n"
	
	def codelines():
		for expression in expressions:
			expression = expression.subs(substitutions)
			codeline = ccode(expression, user_functions=user_functions)
			yield check_code(codeline) + ";\n"
	
	with \
		open( tmpfile(name+".c"            ), "w" ) as mainfile, \
		open( tmpfile(name+"_definitions.c"), "w" ) as deffile:
		
		if helpers:
			mainfile.write("double %s_helper[%i];\n" % (name, len(helpers)))
			arguments += [(name+"_helper","double*")]
		
		if chunk_size < 1:
			for line in chain(helperlines(), codelines()):
				mainfile.write(line)
		else:
			write_in_chunks(helperlines(), mainfile, deffile, name+"_helper", chunk_size, arguments[1:])
			write_in_chunks(codelines()  , mainfile, deffile, name+"code"  , chunk_size, arguments)


def render_template(filename, target, **kwargs):
	folder = path.dirname(__file__)
	env = Environment(loader=FileSystemLoader(folder))
	template = env.get_template(filename)
	with open(target, "w") as codefile:
		codefile.write(template.render(kwargs))


# Numerical tools
# ---------------

# Return the ratio of zero entries in a SymPy matrix
def non_zero_ratio(A):
	return sum(x!=0 for x in A) / len(A)

def random_direction(n):
	vector = np.random.normal(0,1,n)
	return vector/np.linalg.norm(vector)


# Orthonormalise vectors and return their norms after orthogonalisation
def orthonormalise(vectors):
	norms = []
	for i,vector in enumerate(vectors):
		for j in range(i):
			vector -= np.dot( vector, vectors[j] ) * vectors[j]
		norm = np.linalg.norm(vector)
		vector /= norm
		norms.append(norm)
	
	return np.array(norms)
