import json

import ollama
from tqdm import tqdm


MODEL = 'gpt-oss:20b'
COMMENTS_JSONL = 'paper_comments.jsonl'
OUTPUT_JSONL = 'llm_classifications.jsonl'


class PaperExtractedCommentIterator:
    def __init__(self, comments_file: str):
        self.comments_file = comments_file
        # Count lines
        with open(comments_file, 'r', encoding='utf-8') as f:
            self.iteration_count = sum(1 for line in f)
        self.current_paper_index = 0
        self.file_reader = open(comments_file, 'r', encoding='utf-8')
    
    def __iter__(self):
        return self
    
    def __len__(self):
        return self.iteration_count

    def __del__(self):
        self.file_reader.close()

    def __next__(self) -> tuple[int, dict[str, str]]:
        if self.current_paper_index >= self.iteration_count:
            self.file_reader.close()
            raise StopIteration
        content = self.current_paper_index, json.loads(self.file_reader.readline())
        self.current_paper_index += 1
        return content


preprompt = None
with open('preprompt.md', 'r', encoding='utf-8') as f:
    preprompt = f.read()

def ask_local_llm(prompt : str) -> str:
    response = ollama.chat(
        model=MODEL,
        messages=[
            {
                "role": "system",
                "content": preprompt,
            },
            {
                "role": "user",
                "content": prompt,
            }
        ],
    )
    return response["message"]["content"]


paper_iterator = PaperExtractedCommentIterator(COMMENTS_JSONL)
with tqdm(total=len(paper_iterator)) as pbar:
    with open(OUTPUT_JSONL, 'w', encoding='utf-8') as out_file:
        for index, comments in paper_iterator:
            if comments['comments'] == '':
                pbar.update(1)
                continue
            response = ask_local_llm(comments['comments'])
            out_file.write(json.dumps({"name": comments['name'], "response": response}) + "\n")
            out_file.flush()
            pbar.update(1)