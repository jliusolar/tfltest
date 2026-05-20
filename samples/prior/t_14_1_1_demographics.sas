/*****************************************************************************
* Program: t_14_1_1_demographics.sas
* Purpose: Summary of demographic characteristics
*****************************************************************************/

options nodate nonumber missing=' ';

%let outdir = .;
%let outname = t_14_1_1_demographics.rtf;

ods rtf file="&outdir./&outname" style=journal bodytitle;

title1 'Summary of Demographic Characteristics';
title2 'Safety Population';
footnote1 'Percentages are based on the number of subjects in the safety population for each treatment group.';
footnote2 'Age is calculated at informed consent date.';

proc format;
  value $sexfmt
    'M' = 'Male'
    'F' = 'Female';
run;

proc sql;
  create table denom as
  select trt01pn, trt01p, count(distinct usubjid) as denom
  from adam.adsl
  where saffl = 'Y'
  group by trt01pn, trt01p;

  create table age_stats as
  select trt01pn,
         trt01p,
         count(age) as n,
         mean(age) as mean,
         std(age) as sd,
         median(age) as median,
         min(age) as min,
         max(age) as max
  from adam.adsl
  where saffl = 'Y'
  group by trt01pn, trt01p;

  create table sex_counts as
  select trt01pn, trt01p, put(sex, $sexfmt.) as category, count(distinct usubjid) as count
  from adam.adsl
  where saffl = 'Y'
  group by trt01pn, trt01p, calculated category;

  create table race_counts as
  select trt01pn, trt01p, race as category, count(distinct usubjid) as count
  from adam.adsl
  where saffl = 'Y'
  group by trt01pn, trt01p, race;
quit;

data table_rows;
  length section $40 row_label $120 stat $80;
  set age_stats;
  section = 'Age (years)';
  row_label = 'n'; stat = strip(put(n, 8.)); output;
  row_label = 'Mean'; stat = strip(put(mean, 8.1)); output;
  row_label = 'SD'; stat = strip(put(sd, 8.2)); output;
  row_label = 'Median'; stat = strip(put(median, 8.1)); output;
  row_label = 'Min, Max'; stat = cats(put(min, 8.0), ', ', put(max, 8.0)); output;
run;

proc report data=table_rows nowd headline headskip split='|';
  columns section row_label trt01pn, stat;
  define section / group 'Parameter';
  define row_label / group ' ';
  define trt01pn / across 'Treatment';
  define stat / display ' ';
run;

ods rtf close;
