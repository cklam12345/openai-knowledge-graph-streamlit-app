from pathlib import Path
from typing import List
from langchain.chains.openai_functions import create_structured_output_chain
from langchain.chat_models import ChatOpenAI
from langchain.document_loaders import WikipediaLoader, PyPDFLoader, TextLoader, DirectoryLoader
from langchain.docstore.document import Document
from langchain.embeddings.openai import OpenAIEmbeddings
from langchain.graphs import Neo4jGraph
from langchain.prompts import ChatPromptTemplate
from langchain.pydantic_v1 import BaseModel, Field
from langchain.text_splitter import TokenTextSplitter, CharacterTextSplitter
from neo4j.exceptions import ClientError
import os

#for azure ai setup
from openai import AzureOpenAI
api_key = os.getenv("AZURE_OPENAI_API_KEY")  # Retrieve from environment variable
azure_endpoint = os.getenv("AZURE_OPENAI_ENDPOINT")
api_version = "2023-12-01-preview"  # Example, adjust as needed
client = AzureOpenAI(api_key=api_key, api_version=api_version, azure_endpoint=azure_endpoint)




#for executing graph db known as neo4j, the one ! from matrix
graph = Neo4jGraph()

# Load Wikipedia Data
#all_data = WikipediaLoader(query="what is the next best MSR Research project for microsoft continue to invest with similar return as openai").load()
loader = DirectoryLoader('./data/', glob='*.pdf', loader_cls=PyPDFLoader)
all_data = loader.load()

# Embeddings & LLM models
#with open ai public access setup
##embeddings = OpenAIEmbeddings()
##embedding_dimension = 1536
#with azure ai access setup
embeddings = OpenAIEmbeddings(document_model_name="text-embedding-large", openai=client)
embedding_dimension = 1536
llm = ChatOpenAI(temperature=0, openai=client)

# Process All Data
parent_splitter = TokenTextSplitter(chunk_size=512, chunk_overlap=24)
child_splitter = TokenTextSplitter(chunk_size=100, chunk_overlap=24)

# Ingest Parent-Child node pairs
for document in all_data:
    parent_documents = parent_splitter.split_documents([document])
    for i, parent in enumerate(parent_documents):
        child_documents = child_splitter.split_documents([parent])
        params = {
            "parent_text": parent.page_content,
            "parent_id": i,
            "parent_embedding": embeddings.embed_query(parent.page_content),
            "children": [
                {
                    "text": c.page_content,
                    "id": f"{i}-{ic}",
                    "embedding": embeddings.embed_query(c.page_content),
                }
                for ic, c in enumerate(child_documents)
            ],
        }
        # Ingest data
        graph.query(
            """
        MERGE (p:Parent {id: $parent_id})
        SET p.text = $parent_text
        WITH p
        CALL db.create.setVectorProperty(p, 'embedding', $parent_embedding)
        YIELD node
        WITH p 
        UNWIND $children AS child
        MERGE (c:Child {id: child.id})
        SET c.text = child.text
        MERGE (c)<-[:HAS_CHILD]-(p)
        WITH c, child
        CALL db.create.setVectorProperty(c, 'embedding', child.embedding)
        YIELD node
        RETURN count(*)
        """,
            params,
        )
        # Create vector index for child
        try:
            graph.query(
                "CALL db.index.vector.createNodeIndex('parent_document', "
                "'Child', 'embedding', $dimension, 'cosine')",
                {"dimension": embedding_dimension},
            )
        except ClientError:  # already exists
            pass
        # Create vector index for parents
        try:
            graph.query(
                "CALL db.index.vector.createNodeIndex('typical_rag', "
                "'Parent', 'embedding', $dimension, 'cosine')",
                {"dimension": embedding_dimension},
            )
        except ClientError:  # already exists
            pass
    # Ingest hypothethical questions


class Questions(BaseModel):
    """Generating hypothetical questions about text."""

    questions: List[str] = Field(
        ...,
        description=(
            "Generated hypothetical questions based on " "the information from the text"
        ),
    )


questions_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            (
                "You are generating hypothetical questions based on the information "
                "found in the text. Make sure to provide full context in the generated "
                "questions."
            ),
        ),
        (
            "human",
            (
                "Use the given format to generate hypothetical questions from the "
                "following input: {input}"
            ),
        ),
    ]
)

question_chain = create_structured_output_chain(Questions, llm, questions_prompt)

for i, parent in enumerate(parent_documents):
    questions = question_chain.run(parent.page_content).questions
    params = {
        "parent_id": i,
        "questions": [
            {"text": q, "id": f"{i}-{iq}", "embedding": embeddings.embed_query(q)}
            for iq, q in enumerate(questions)
            if q
        ],
    }
    graph.query(
        """
    MERGE (p:Parent {id: $parent_id})
    WITH p
    UNWIND $questions AS question
    CREATE (q:Question {id: question.id})
    SET q.text = question.text
    MERGE (q)<-[:HAS_QUESTION]-(p)
    WITH q, question
    CALL db.create.setVectorProperty(q, 'embedding', question.embedding)
    YIELD node
    RETURN count(*)
    """,
        params,
    )
    # Create vector index
    try:
        graph.query(
            "CALL db.index.vector.createNodeIndex('hypothetical_questions', "
            "'Question', 'embedding', $dimension, 'cosine')",
            {"dimension": embedding_dimension},
        )
    except ClientError:  # already exists
        pass

# Ingest summaries

summary_prompt = ChatPromptTemplate.from_messages(
    [
        (
            "system",
            (
                "You are generating concise and accurate summaries based on the "
                "information found in the text."
            ),
        ),
        (
            "human",
            ("Generate a summary of the following input: {question}\n" "Summary:"),
        ),
    ]
)

summary_chain = summary_prompt | llm

for i, parent in enumerate(parent_documents):
    summary = summary_chain.invoke({"question": parent.page_content}).content
    params = {
        "parent_id": i,
        "summary": summary,
        "embedding": embeddings.embed_query(summary),
    }
    graph.query(
        """
    MERGE (p:Parent {id: $parent_id})
    MERGE (p)-[:HAS_SUMMARY]->(s:Summary)
    SET s.text = $summary
    WITH s
    CALL db.create.setVectorProperty(s, 'embedding', $embedding)
    YIELD node
    RETURN count(*)
    """,
        params,
    )
    # Create vector index
    try:
        graph.query(
            "CALL db.index.vector.createNodeIndex('summary', "
            "'Summary', 'embedding', $dimension, 'cosine')",
            {"dimension": embedding_dimension},
        )
    except ClientError:  # already exists
        pass
