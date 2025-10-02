import json
import hashlib
import random
from transformers import BertTokenizer

# Initialize BERT tokenizer
tokenizer = BertTokenizer.from_pretrained('bert-base-uncased')

# Input and output file names
input_file = 'FINAL.json'
output_file = 'enhanced_data.json'

# Counters for sensitive entries
sensitive_true = 0
sensitive_false = 0

# Read the JSON data
with open(input_file, 'r') as f:
    data = json.load(f)

# Process each entry
for entry in data:
    # Count sensitive flag
    if entry.get('flagged') is True:
        sensitive_true += 1
    else:
        sensitive_false += 1

    # Calculate SHA-256 checksum of the comment
    comment_text = entry.get('comments', '')
    sha_256 = hashlib.sha256(comment_text.encode('utf-8')).hexdigest()
    entry['sha_256'] = sha_256

    # Calculate BERT token length
    tokens = tokenizer.tokenize(comment_text)
    entry['token_length'] = len(tokens)

# Print statistics
print(f"Sensitive = true: {sensitive_true}")
print(f"Sensitive = false: {sensitive_false}")

# Scramble (shuffle) the dataset
random.shuffle(data)

# Write enhanced and shuffled data to a new file
with open(output_file, 'w') as f:
    json.dump(data, f, indent=2)

print(f"Shuffled and enhanced data saved to {output_file}")
