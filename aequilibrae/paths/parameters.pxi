import numpy as np 

DTYPE = np.float64
ctypedef np.float64_t DTYPE_t

ITYPE = np.int64
ctypedef np.int64_t ITYPE_t

# EPS is the precision of DTYPE
cdef DTYPE_t DTYPE_EPS = 1E-15

# NULL_IDX is the index used in predecessor matrices to store a non-path
cdef ITYPE_t NULL_IDX = 18446744073709551615

cdef double INFINITE = 1.79769313e+308

VERSION = 0.6
MINOR_VRSN = 5
binary_version = "0.6.2"
release_name = "Rio de Janeiro"