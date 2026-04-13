/* collect_site_ids.sas
   Expects via initstmt:
     input_lib_path = directory containing *.sas7bdat
     output_csv     = output csv path
*/

libname inlib "&input_lib_path";

/* Discover remap columns across all tables in inlib.
   Mirrors rules.py:
     remap_id    — name ends with 'id'
     remap_label — site, pro_response_text, vx_lot_num
     redact      — excluded (^raw_ | trial_invite_code | provider_npi | result_text$ | zip9$)
   alias_attributes: provider-role columns use 'providerid' as the canonical key. */
proc sql;
    create table remap_cols_found as
    select
        memname,
        name,
        case
            when lowcase(name) in ('medadmin_providerid', 'obsgen_providerid',
                                   'obsclin_providerid',  'rx_providerid',
                                   'vx_providerid')        then 'providerid'
            else lowcase(name)
        end as column_key length=64
    from dictionary.columns
    where libname='INLIB'
      and prxmatch('/id$/i', strip(name))
      and lowcase(strip(name)) not in ('participant id');
quit;

data id_values_long;
    length table_name $64 column $64 original_value $256;
    stop;
run;

data _null_;
    set remap_cols_found;
    length code $2000;

    code = cats(
        'data _one_; set inlib.', strip(memname), '(keep=', strip(name), '); ',
        'length table_name $64 column $64 original_value $256; ',
        'if not missing(', strip(name), ') then do; ',
        'table_name="', strip(memname), '"; ',
        'column="', strip(column_key), '"; ',
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
