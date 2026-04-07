/* collect_site_ids.sas
   Expects via initstmt:
     input_lib_path = directory containing *.sas7bdat
     output_csv     = output csv path
*/

%let id_columns =
"addressid", "conditionid",
"diagnosisid", "dispensingid", "encounterid",
"facilityid", "geocodeid", "immunizationid",
"lab_facilityid", "lab_result_cm_id", "labhistoryid",
"medadmin_providerid", "medadminid",
"obsclin_providerid", "obsclinid", "obsgen_providerid",
"obsgenid", "org_patid",
"patid", "person_id", "prescribingid",
"pro_cm_id", "proceduresid", "providerid",
"raw_siteid", "rx_providerid", "trial_siteid",
"trialid", "visit_id", "vitalid",
"vx_providerid",
"med_id";

libname inlib "&input_lib_path";

proc sql;
    create table id_columns_found as
    select
        memname,
        name,
        lowcase(name) as column length=64
    from dictionary.columns
    where libname='INLIB'
      and lowcase(name) in (&id_columns);
quit;

data id_values_long;
    length table_name $64 column $64 original_value $256;
    stop;
run;

data _null_;
    set id_columns_found;
    length code $2000;

    code = cats(
        'data _one_; set inlib.', strip(memname), '(keep=', strip(name), '); ',
        'length table_name $64 column $64 original_value $256; ',
        'if not missing(', strip(name), ') then do; ',
        'table_name="', strip(memname), '"; ',
        'column="', strip(column), '"; ',
        'original_value=cats(', strip(name), '); output; end; ',
        'keep table_name column original_value; run; ',
        'proc append base=id_values_long data=_one_ force; run; ',
        'proc datasets lib=work nolist; delete _one_; quit;'
    );

    call execute(code);
run;

proc sort data=id_values_long nodupkey;
    by column original_value;
run;

proc export data=id_values_long
    outfile="&output_csv"
    dbms=csv
    replace;
run;
