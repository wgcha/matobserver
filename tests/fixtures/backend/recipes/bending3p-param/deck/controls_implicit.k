*KEYWORD
$ implicit static, auto time step capped by &mo_dtmax (recipe.md §3.2)
*CONTROL_IMPLICIT_GENERAL
$#  imflag       dt0
         1 &mo_dtmax
*CONTROL_IMPLICIT_AUTO
$#   iauto    iteopt    itewin     dtmin     dtmax
         1        11         5       0.0 &mo_dtmax
*CONTROL_IMPLICIT_SOLUTION
$#  nsolvr    ilimit    maxref     dctol     ectol     rctol     lstol    abstol
        12        11        15     0.001      0.01       0.0       0.9    1.0E-10
*END
