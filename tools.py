import PyPDF2
import io
import logging
from typing import Tuple, Optional, List
import re
import pandas as pd
import uuid
from langchain.text_splitter import CharacterTextSplitter
from sentence_transformers import SentenceTransformer
import numpy as np
import json
from google.cloud import bigquery
import google.generativeai as genai

class Tools:
    def __init__(self, chunk_size: int, overlap: int, embedding_model: str = "all-MiniLM-L6-v2", 
                 project_id: str = "flaviosrag", dataset_id: str = "document_chunks", table_id: str = "vectorized_chunks",
                 feedback_table_id: str = "feedback_table"):
        self.gemini_api_key = self.__get_api_key()
        self.chunk_size = chunk_size
        self.overlap = overlap
        self.project_id = project_id
        self.dataset_id = dataset_id
        self.table_id = table_id
        self.feedback_table_id = feedback_table_id
        self.client = bigquery.Client()
        self.logger = self.__setup_logger()
        self.table_ref = self.__get_table_ref(self.project_id, self.dataset_id, self.table_id)
        self.feedback_table_ref = self.__get_table_ref(self.project_id, self.dataset_id, self.feedback_table_id)

        genai.configure(api_key = self.gemini_api_key)


    def __setup_logger(self) -> logging.Logger:
        logger = logging.getLogger("udf_logger")
        return logger


    def __get_table_ref(self, project_id, dataset_id, table_id) -> str:
        return f"{project_id}.{dataset_id}.{table_id}"


    def __get_api_key(self, filepath: str = "config.txt") -> str:
        with open(filepath, "r") as file:
            for line in file:
                if line.startswith("GEMINI_API_KEY="):
                    return line.strip().split("=")[1]


    def document_exists(self, document_name: str) -> bool:
        query = f"""
        SELECT IF(EXISTS(
            SELECT 1 FROM `{self.table_ref}` 
            WHERE DOCUMENT_NAME = @document_name
        ), TRUE, FALSE) AS exists_flag
        """
        try:
            job_config = bigquery.QueryJobConfig(
                query_parameters=[
                    bigquery.ScalarQueryParameter("document_name", "STRING", document_name)
                ]
            )
            result = self.client.query(query, job_config=job_config).result()
            
            row = next(result, None)  # Use next() to avoid unnecessary looping
            return row["exists_flag"] if row else False  # Ensure a return value even if no rows
        except Exception as e:
            self.logger.warning(f"Failed to check document existence: {e}")
            return False


    def pdf_reader(self, file_path: str) -> tuple[str | None, list[int] | None]:
        self.logger.info(f"Opening {file_path}.")

        try:
            with open(file_path, "rb") as f:
                buffer = io.BytesIO(f.read())

            reader = PyPDF2.PdfReader(buffer)
        except Exception as e:
            self.logger.error(f"Failed to open {file_path}: {e}.")
            return None, None

        metadata = reader.metadata
        metadata_text = f"{metadata.title or ''} {metadata.author or ''}".strip() if metadata else ""

        text = ""
        page_breaks = [0]

        for i, page in enumerate(reader.pages):
            try:
                page_text = page.extract_text()
                if page_text:
                    page_text = page_text.replace("\n", " ").replace("\x00", "")

                    if metadata_text and page_text.startswith(metadata_text):
                        page_text = page_text[len(metadata_text):].strip()

                    text += page_text + " "
                    page_breaks.append(len(text))
                else:
                    self.logger.warning(f"Empty text extracted from {file_path}, page {i + 1}.")
            except Exception as e:
                self.logger.warning(f"Unable to extract text from {file_path}, page {i + 1}: {e}")

        if not text.strip() or len(text) < 1500:
            self.logger.info(f"File {file_path} is empty or has insufficient text for extraction.")
            return None, None

        return text, page_breaks


    def get_embedding(self, text: str) -> List[float]:
        try:
            response = genai.embed_content(model = "models/embedding-001", content = text)
            return response["embedding"] if "embedding" in response else []
        except Exception as e:
            self.logger.error(f"Failed to fetch embedding: {e}")
            return []

    
    def get_embedding_batch(self, chunk_texts: List[str]) -> List[List[float]]:
        try:
            # Hypothetical batch call; adjust accordingly for the actual endpoint
            response = genai.embed_content_batch(
                model="models/embedding-001", 
                contents=chunk_texts
            )
            return [r["embedding"] for r in response["embeddings"]]
        except Exception as e:
            self.logger.error(f"Failed to fetch batch embeddings: {e}")
            return [[] for _ in chunk_texts]


    def get_embedding_batch(self, chunk_texts: List[str]) -> List[List[float]]:
        try:
            # Hypothetical batch call; adjust accordingly for the actual endpoint
            response = genai.embed_content_batch(
                model="models/embedding-001", 
                contents=chunk_texts
            )
            return [r["embedding"] for r in response["embeddings"]]
        except Exception as e:
            self.logger.error(f"Failed to fetch batch embeddings: {e}")
            return [[] for _ in chunk_texts]


    def text_chunker(self, text: str, document_name: str) -> pd.DataFrame:
        self.logger.info("Chunking text")
        text = re.sub(r"\s+", " ", text).strip()

        splitter = CharacterTextSplitter(separator = " ", chunk_size=self.chunk_size, chunk_overlap=self.overlap)
        chunks = splitter.split_text(text)
        self.logger.info(f"Text split into {len(chunks)} chunks.")

        # Compute embeddings
        embeddings = self.get_embedding_batch(chunks)

        # Create DataFrame
        df = pd.DataFrame({
            "uuid": [str(uuid.uuid4()) for _ in range(len(chunks))],
            "chunk": chunks,
            "embedding": embeddings,
            "document_name": [document_name] * len(chunks)
        })

        return df


    def push_df_to_db(self, df: pd.DataFrame, document_name: str):
        rows = [
            {
                "UUID": row["uuid"],
                "CHUNK": row["chunk"],
                "EMBEDDING": json.dumps(row["embedding"]),  # Store embeddings as JSON
                "DOCUMENT_NAME": row["document_name"]  # New field
            }
            for _, row in df.iterrows()
        ]

        errors = self.client.insert_rows_json(self.table_ref, rows)
        if errors:
            print(f"Failed to insert rows: {errors}")
        else:
            print(f"Successfully inserted {len(rows)} rows into BigQuery")

    def push_feedback(self, df: pd.DataFrame):
        rows = [
            {
                "RATING": row["rating"],
                "NOTES": row["notes"],
                "QUESTION": row["question"],
                "ANSWER": row["answer"],
                "DOCUMENT_NAME": row["document_name"]
            }
            for _, row in df.iterrows()
        ]
        errors = self.client.insert_rows_json(self.feedback_table_ref, rows)
        if errors:
            print(f"Failed to insert rows: {errors} into feedback")
        else:
            print(f"Successfully inserted {len(rows)} rows into BigQuery feedback")