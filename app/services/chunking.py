import logging
from typing import List, Any

logger = logging.getLogger(__name__)

class RecursiveCharacterTextSplitter:
    """
    A robust, pure-python implementation of LangChain's RecursiveCharacterTextSplitter.
    Recursively splits text using a list of separators (default: ["\\n\\n", "\\n", " ", ""])
    to keep semantic boundaries where possible while adhering to chunk_size and chunk_overlap.
    """
    def __init__(
        self,
        chunk_size: int = 500,
        chunk_overlap: int = 50,
        separators: List[str] = None,
        length_function: Any = len
    ):
        self.chunk_size = chunk_size
        self.chunk_overlap = chunk_overlap
        self.separators = separators or ["\n\n", "\n", " ", ""]
        self.length_function = length_function

    def split_text(self, text: str) -> List[str]:
        if not text or not text.strip():
            return []
        return self._split_text(text, self.separators)

    def _split_text(self, text: str, separators: List[str]) -> List[str]:
        if self.length_function(text) <= self.chunk_size:
            return [text]

        # Find the best separator to use
        separator = separators[-1] if separators else ""
        new_separators = []
        for i, sep in enumerate(separators):
            if sep == "":
                separator = sep
                new_separators = separators[i+1:]
                break
            if sep in text:
                separator = sep
                new_separators = separators[i+1:]
                break

        # Split text by separator
        if separator:
            splits = text.split(separator)
        else:
            splits = list(text)

        # Merge splits into chunks
        chunks = []
        current_doc = []
        current_len = 0

        for split in splits:
            split_len = self.length_function(split)
            
            # If a single split is larger than chunk_size, split it recursively
            if split_len > self.chunk_size:
                if current_doc:
                    chunks.append(separator.join(current_doc))
                    current_doc = []
                    current_len = 0
                
                # Recursively split the long block
                sub_splits = self._split_text(split, new_separators)
                for sub_split in sub_splits:
                    sub_len = self.length_function(sub_split)
                    if current_len + sub_len + (len(separator) if current_doc else 0) <= self.chunk_size:
                        current_doc.append(sub_split)
                        current_len += sub_len + (len(separator) if len(current_doc) > 1 else 0)
                    else:
                        if current_doc:
                            chunks.append(separator.join(current_doc))
                        current_doc = [sub_split]
                        current_len = sub_len
            else:
                # Normal split path
                join_len = len(separator) if current_doc else 0
                if current_len + split_len + join_len <= self.chunk_size:
                    current_doc.append(split)
                    current_len += split_len + join_len
                else:
                    if current_doc:
                        chunks.append(separator.join(current_doc))
                    
                    # Handle overlap: keep previous splits that fit within overlap
                    overlap_doc = []
                    overlap_len = 0
                    for prev in reversed(current_doc):
                        prev_len = self.length_function(prev)
                        prev_join = len(separator) if overlap_doc else 0
                        if overlap_len + prev_len + prev_join <= self.chunk_overlap:
                            overlap_doc.insert(0, prev)
                            overlap_len += prev_len + prev_join
                        else:
                            break
                    
                    current_doc = overlap_doc
                    current_len = overlap_len
                    
                    # Add current split
                    join_len = len(separator) if current_doc else 0
                    current_doc.append(split)
                    current_len += split_len + join_len

        if current_doc:
            chunks.append(separator.join(current_doc))

        return [c.strip() for c in chunks if c.strip()]


def chunk_text(text: str, chunk_size: int = 500, chunk_overlap: int = 50) -> List[str]:
    """
    Splits text into chunks of specified size and overlap using RecursiveCharacterTextSplitter.
    """
    if not text or not text.strip():
        return []
    
    try:
        splitter = RecursiveCharacterTextSplitter(
            chunk_size=chunk_size,
            chunk_overlap=chunk_overlap,
            length_function=len
        )
        return splitter.split_text(text)
    except Exception as e:
        logger.error(f"Error chunking text: {e}")
        return [text]
