#!/usr/bin/env python3
"""
Fix malformed events.ffmpeg.jsonl file by converting multi-line JSON to single-line JSONL format.
"""
import json
import sys
from pathlib import Path

def fix_ffmpeg_jsonl(input_file, output_file):
    """Convert multi-line JSON records to single-line JSONL format."""
    records = []
    current_record = ""
    brace_count = 0
    
    with open(input_file, 'r', encoding='utf-8') as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
                
            current_record += line
            
            # Count braces to detect complete JSON objects
            brace_count += line.count('{') - line.count('}')
            
            if brace_count == 0 and current_record:
                # We have a complete JSON object
                try:
                    obj = json.loads(current_record)
                    records.append(obj)
                    current_record = ""
                except json.JSONDecodeError as e:
                    print(f"Error parsing JSON: {e}")
                    print(f"Content: {current_record}")
                    current_record = ""
    
    # Write records in proper JSONL format
    with open(output_file, 'w', encoding='utf-8') as f:
        for record in records:
            f.write(json.dumps(record, ensure_ascii=False) + '\n')
    
    print(f"Fixed {len(records)} records from {input_file} to {output_file}")

if __name__ == "__main__":
    if len(sys.argv) != 3:
        print("Usage: python3 fix_ffmpeg_jsonl.py <input_file> <output_file>")
        sys.exit(1)
    
    input_file = sys.argv[1]
    output_file = sys.argv[2]
    
    fix_ffmpeg_jsonl(input_file, output_file)
