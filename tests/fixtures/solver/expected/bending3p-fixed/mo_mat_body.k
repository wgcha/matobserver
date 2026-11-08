*KEYWORD
$ matobserver slot=body keyword=MAT_024 units=mm-N-s-tonne
*PART
matobserver slot body pid 1
$#     pid     secid       mid     eosid      hgid      grav    adpopt      tmid
         1       101       101         0         0         0         0         0
*SECTION_SOLID
$#   secid    elform       aet
       101         2         0
*MAT_PIECEWISE_LINEAR_PLASTICITY
$#     mid        ro         e        pr      sigy      etan      fail      tdel
       101    1.2E-9    2000.0      0.38      40.0       0.0
$#       c         p      lcss      lcsr        vp
       0.0       0.0      1101         0       0.0
$#    eps1      eps2      eps3      eps4      eps5      eps6      eps7      eps8
         0         0         0         0         0         0         0         0
$#     es1       es2       es3       es4       es5       es6       es7       es8
         0         0         0         0         0         0         0         0
*DEFINE_CURVE_TITLE
true stress [MPa] vs effective plastic strain
$#    lcid      sidr       sfa       sfo      offa      offo    dattyp
      1101         0       1.0       1.0       0.0       0.0         0
$#                a1                  o1
  0.000000000000E+00  4.000000000000E+01
  1.000000000000E-02  4.800000000000E+01
  5.000000000000E-02  6.000000000000E+01
*END
