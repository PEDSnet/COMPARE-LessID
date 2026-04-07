#!/bin/bash
set -e

# Sample usage:
#./run.sh "./*.sas7bdat" "./results" "/path/to/mapping.csv" sas7bdat 0

input_pattern="$1"
output_dir="$2"
mapping_path="$3"
output_type="${4:-sas7bdat}"
date_shift_days="${5:-0}"

mkdir -p $output_dir

echo "Input Pattern: $input_pattern"
echo "Output Dir: $output_dir"
echo "Mapping Path: $mapping_path"
echo "Output Type: $output_type"
echo "Days Shift: $date_shift_days"

touch $output_dir/_lessid_processing

# Count total files first for progress display
total=0
for f in $input_pattern; do [ -f "$f" ] && total=$((total + 1)); done
echo "Total files to process: $total"

current=0
for file in $input_pattern; do
    [ -f "$file" ] || continue
    current=$((current + 1))
    file_basename="$(basename "$file")"
    file_basename_wo_extension=$(echo "$file_basename" | cut -f 1 -d '.')

    echo "[$current/$total] $file_basename"

    output_path="$output_dir/$file_basename_wo_extension.$output_type"
    echo "Outputting to: $output_path" 


    sas_log_path="$output_dir/log_$file_basename_wo_extension.log"
    sas_lst_path="$output_dir/lst_$file_basename_wo_extension.lst"
    echo "Logging to $sas_log_path"
    echo "Save lst to $sas_lst_path"
    init_stmt="%let mapping_path = $mapping_path; 
    %let input_path = $file; 
    %let output_path = $output_path;
    %let output_type = $output_type;
    %let date_shift_days = $date_shift_days;
"
    echo "Init Statement: $init_stmt"
    sas -print $sas_lst_path -log $sas_log_path -initstmt "$init_stmt" ./sas/lessid.sas
done

rm $output_dir/_lessid_processing
touch $output_dir/_lessid_completed

echo "Processed Successfully!"
