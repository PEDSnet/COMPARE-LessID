/* Expecting input_path, mapping_path, output_type, output_path set in init_stmt */
/* output_type can be 'sas7bdat' or 'csv', default to sas7bdat */

%macro lessid;

/* Determine date_shift_days, default to 0 if not provided */
%let date_shift_days = %sysfunc(ifc(%symexist(date_shift_days), &date_shift_days, 0));

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

/* Read original data */
data original_data;
    set "&input_path";
run;

/* Read mapping csv (column, original_value, new_id) */
proc import datafile="&mapping_path"
    out=mapping_data
    dbms=csv
    replace;
    guessingrows=max;
run;

/* Normalize mapping keys in-place */
data mapping_data;
    set mapping_data;
    length column_key $64 original_key $256;
    column_key = lowcase(strip(column));
    original_key = strip(original_value);
    keep column_key original_key new_id;
run;

/* Find which ID columns actually exist in this dataset */
proc sql noprint;
    select lowcase(name)
        into :id_cols_found separated by ' '
    from dictionary.columns
    where libname='WORK'
          and memname='ORIGINAL_DATA'
          and lowcase(name) in (&id_columns);
quit;
%let id_col_count = &sqlobs;

/* Apply mapping via a hash object — O(1) per lookup regardless of mapping size.
   The hash is loaded once into memory; every row then does a direct key lookup.
   Unmapped non-null values become missing (visible as data quality issue). */
%if &id_col_count > 0 %then %do;

    data mapped_data;
        if _N_ = 1 then do;
            /* Pre-declare hash variables to avoid uninitialized warnings */
            length column_key $64 original_key $256 new_id $64;
            call missing(column_key, original_key, new_id);
            /* hashexp:20 → 2^20 buckets, good for up to ~3.5M entries */
            declare hash h(dataset:'mapping_data', hashexp:20);
            h.defineKey('column_key', 'original_key');
            h.defineData('new_id');
            h.defineDone();
        end;

        set original_data;

        %let _i = 1;
        %do %while (%scan(&id_cols_found, &_i, %str( )) ne );
            %let _col = %scan(&id_cols_found, &_i, %str( ));
            if not missing(&_col) then do;
                column_key = "&_col";
                original_key = cats(&_col);
                if h.find() = 0 then &_col = new_id;
                else call missing(&_col);
            end;
            %let _i = %eval(&_i + 1);
        %end;

        drop column_key original_key new_id;
    run;

%end;
%else %do;
    /* No ID columns present — pass through unchanged */
    data mapped_data;
        set original_data;
    run;
%end;

/* Optional deterministic date shift based on mapped patid */
proc sql noprint;
    select count(*) into :patid_exists
    from dictionary.columns
    where libname='WORK'
          and memname='MAPPED_DATA'
          and lowcase(name)='patid';
quit;

proc sql noprint;
    select name
        into :date_cols separated by ' '
    from dictionary.columns
    where libname='WORK'
          and memname='MAPPED_DATA'
          and lowcase(name) like '%_date'
          and type='num';
quit;

%let date_count = &sqlobs;

%if &date_count > 0 and &date_shift_days > 0 %then %do;
    data mapped_data;
        set mapped_data;

        if &patid_exists > 0 then
            day_shift = mod(abs(crc32(cats(patid))), &date_shift_days * 2 + 1) - &date_shift_days;
        else
            day_shift = 0;

        array date_vars {*} &date_cols;
        do i = 1 to dim(date_vars);
            if not missing(date_vars{i}) then do;
                date_vars{i} = intnx('day', date_vars{i}, day_shift);
                date_vars{i} = min(max(date_vars{i}, '01JAN1900'd), '31DEC9999'd);
            end;
        end;

        drop day_shift i;
    run;

    proc datasets lib=work nolist;
        modify mapped_data;
        format &date_cols YYMMDD10.;
    quit;
%end;

/* Determine output type and write output */
%let output_type = %sysfunc(ifc(%superq(output_type)=, sas7bdat, %lowcase(%superq(output_type))));

%if &output_type = sas7bdat %then %do;
    data "&output_path" (compress=yes);
        set mapped_data;
    run;
%end;

%if &output_type = csv %then %do;
    proc export data=mapped_data
        outfile="&output_path"
        dbms=csv
        replace;
    run;
%end;

%mend lessid;
%lessid;
