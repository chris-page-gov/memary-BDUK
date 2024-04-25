import os
import logging
import geocoder
import googlemaps
from dotenv import load_dotenv
from llama_index.core import (KnowledgeGraphIndex, Settings,
                              SimpleDirectoryReader, StorageContext)
from llama_index.core.agent import ReActAgent
from llama_index.core.llms import ChatMessage
from llama_index.core.multi_modal_llms.generic_utils import load_image_urls
from llama_index.core.query_engine import RetrieverQueryEngine
from llama_index.core.retrievers import KnowledgeGraphRAGRetriever
from llama_index.core.tools import FunctionTool
from llama_index.graph_stores.neo4j import Neo4jGraphStore
from llama_index.llms.openai import OpenAI
from llama_index.llms.perplexity import Perplexity
from llama_index.multi_modal_llms.openai import OpenAIMultiModal

from src.agent.data_types import Message
from src.agent.llm_api.tools import openai_chat_completions_request
from src.memory import EntityKnowledgeStore, MemoryStream
from src.synonym_expand.synonym import custom_synonym_expand_fn

MAX_ENTITIES_FROM_KG = 5
ENTITY_EXCEPTIONS = ["Unknown relation"]
# ChatGPT token limits
CONTEXT_LENGTH = 4096
EVICTION_RATE = 0.7
NONEVICTION_LENGTH = 5

def generate_string(entities):
    cypher_query = 'MATCH p = (n) - [*1 .. 2] - ()\n'
    cypher_query += 'WHERE n.id IN ' + str(entities) + '\n'
    cypher_query += 'RETURN p'

    return cypher_query


class Agent(object):
    """Agent manages the RAG model, the ReAct agent, and the memory stream."""

    def __init__(
        self,
        name,
        memory_stream_json,
        entity_knowledge_store_json,
        system_persona_txt,
        user_persona_txt,
        past_chat_json,
    ):
        load_dotenv()
        # getting necessary API keys
        os.environ["OPENAI_API_KEY"] = os.getenv("OPENAI_API_KEY")
        googlemaps_api_key = os.getenv("GOOGLEMAPS_API_KEY")
        pplx_api_key = os.getenv("PERPLEXITY_API_KEY")

        # Neo4j credentials
        self.neo4j_username = "neo4j"
        self.neo4j_password = os.getenv("NEO4J_PW")
        self.neo4j_url = os.getenv("NEO4J_URL")
        database = "neo4j"

        # initialize APIs
        # OpenAI API
        self.model = "gpt-3.5-turbo"
        self.openai_api_key = os.environ["OPENAI_API_KEY"]
        self.model_endpoint = 'https://api.openai.com/v1'
        self.openai_mm_llm = OpenAIMultiModal(
            model="gpt-4-vision-preview",
            api_key=os.getenv("OPENAI_KEY"),
            max_new_tokens=300,
        )
        llm = OpenAI(model="gpt-3.5-turbo-instruct")
        self.query_llm = Perplexity(
            api_key=pplx_api_key, model="mistral-7b-instruct", temperature=0.5
        )
        self.gmaps = googlemaps.Client(key=googlemaps_api_key)
        Settings.llm = llm
        Settings.chunk_size = 512

        # initialize Neo4j graph resources
        graph_store = Neo4jGraphStore(
            username=self.neo4j_username,
            password=self.neo4j_password,
            url=self.neo4j_url,
            database=database,
        )

        self.storage_context = StorageContext.from_defaults(graph_store=graph_store)
        graph_rag_retriever = KnowledgeGraphRAGRetriever(
            storage_context=self.storage_context,
            verbose=True,
            llm=llm,
            retriever_mode="keyword",
            synonym_expand_fn=custom_synonym_expand_fn,
        )

        self.query_engine = RetrieverQueryEngine.from_args(
            graph_rag_retriever,
        )

        search_tool = FunctionTool.from_defaults(fn=self.search)
        locate_tool = FunctionTool.from_defaults(fn=self.locate)
        vision_tool = FunctionTool.from_defaults(fn=self.vision)

        self.routing_agent = ReActAgent.from_tools(
            [search_tool, locate_tool, vision_tool], llm=llm, verbose=True
        )

        self.memory_stream = MemoryStream(memory_stream_json)
        self.entity_knowledge_store = EntityKnowledgeStore(entity_knowledge_store_json)

        self.message = Message(system_persona_txt, user_persona_txt, past_chat_json, self.model)

    def __str__(self):
        return f"Agent {self.name}"

    def external_query(self, query: str):
        messages_dict = [
            {"role": "system", "content": "Be precise and concise."},
            {"role": "user", "content": query},
        ]
        messages = [ChatMessage(**msg) for msg in messages_dict]
        external_response = self.query_llm.chat(messages)

        return str(external_response)

    def search(self, query: str) -> str:
        """Search the knowledge graph or perform search on the web if information is not present in the knowledge graph"""
        response = self.query_engine.query(query)

        if response.metadata is None:
            return self.external_query(query)
        else:
            return response

    def locate(self, query: str) -> str:
        """Finds the current geographical location"""
        location = geocoder.ip("me")
        lattitude, longitude = location.latlng[0], location.latlng[1]

        reverse_geocode_result = self.gmaps.reverse_geocode((lattitude, longitude))
        formatted_address = reverse_geocode_result[0]["formatted_address"]
        return "Your address is" + formatted_address

    def vision(self, query: str, img_url: str) -> str:
        """Uses computer vision to process the image specified by the image url and answers the question based on the CV results"""
        img_docs = load_image_urls([img_url])
        response = self.openai_mm_llm.complete(prompt=query, image_documents=img_docs)
        return response

    def query(self, query: str) -> str:
        # get the response from react agent
        response = self.routing_agent.chat(query)
        self.routing_agent.reset()
        # write response to file for KG writeback
        with open("data/external_response.txt", "w") as f:
            print(response, file=f)
        # write back to the KG
        self.write_back()
        return response

    def write_back(self):
        documents = SimpleDirectoryReader(
            input_files=["data/external_response.txt"]
        ).load_data()

        KnowledgeGraphIndex.from_documents(
            documents,
            storage_context=self.storage_context,
            max_triplets_per_chunk=8,
        )

    def check_KG(self, query: str) -> bool:
        """Check if the query is in the knowledge graph.

        Args:
            query (str): query to check in the knowledge graph

        Returns:
            bool: True if the query is in the knowledge graph, False otherwise
        """
        response = self.query_engine.query(query)

        if response.metadata is None:
            return False
        return generate_string(
            list(list(response.metadata.values())[0]["kg_rel_map"].keys())
        )

    def _change_llm_message_chatgpt(self) -> dict:
        """Change the llm_message to chatgpt format.

        Returns:
            dict: llm_message in chatgpt format
        """
        llm_message_chatgpt = self.message.llm_message
        llm_message_chatgpt["messages"] = [
            context.to_dict() for context in self.message.llm_message["messages"]
        ]
        llm_message_chatgpt["messages"].append(
            {
                "role": "user",
                "content": "Memory Stream:"
                + str(
                    [
                        memory.to_dict()
                        for memory in llm_message_chatgpt.pop("memory_stream")
                    ]
                ),
            }
        )
        llm_message_chatgpt["messages"].append(
            {
                "role": "user",
                "content": "Knowledge Entity Store:"
                + str(
                    [
                        entity.to_dict()
                        for entity in llm_message_chatgpt.pop(
                            "knowledge_entity_store"
                        )
                    ]
                ),
            }
        )
        return llm_message_chatgpt

    def _summarize_contexts(self, total_tokens: int):
        """Summarize the contexts.

        Args:
            total_tokens (int): total tokens in the response
        """
        contexts = self.message.llm_message["messages"]
        if len(contexts) > 2 + NONEVICTION_LENGTH:
            contexts = contexts[2:-NONEVICTION_LENGTH]
            del self.message.llm_message["messages"][2:-NONEVICTION_LENGTH]
        else:
            contexts = contexts[2:]
            del self.message.llm_message["messages"][2:]

        llm_message_chatgpt = {
            "model": self.model,
            "messages": "Summarize these previous conversations into 50 words" + contexts,
        }
        response, _ = self._get_gpt_response(llm_message_chatgpt)
        summarized_message = {
            "role": "assistant",
            "content": "Summarized past conversation:" + response,
        }
        self.message.llm_message["messages"].insert(2, summarized_message)
        logging.info(f"Contexts summarized successfully. \n summary: {summarized_message}")
        logging.info(f"Total tokens after eviction: {total_tokens*EVICTION_RATE}")

    def _get_gpt_response(self, llm_message_chatgpt: str) -> str:
        """Get response from the GPT model.

        Args:
            llm_message_chatgpt (str): query to get response for

        Returns:
            str: response from the GPT model
        """
        response = openai_chat_completions_request(
            self.model_endpoint, self.openai_api_key, llm_message_chatgpt
        )
        total_tokens = response["usage"]["total_tokens"]
        response = str(response["choices"][0]["message"]["content"])
        return response, total_tokens

    def get_response(self) -> str:
        """Get response from the RAG model.

        Returns:
            str: response from the RAG model
        """
        llm_message_chatgpt = self._change_llm_message_chatgpt()
        response, total_tokens = self._get_gpt_response(llm_message_chatgpt)
        if total_tokens > CONTEXT_LENGTH*EVICTION_RATE:
            logging.info("Evicting and summarizing contexts")
            self._summarize_contexts(total_tokens)

        return response

    def get_routing_agent_response(self, query, return_entity=False):
        """Get response from the ReAct."""
        response = str(self.query(query))

        if return_entity:
            # the query above already adds final response to KG so entities will be present in the KG
            return response, self.get_entity(self.query_engine.retrieve(query))
        return response

    def get_entity(self, retrieve) -> list[str]:
        """retrieve is a list of QueryBundle objects.
        A retrieved QueryBundle object has a "node" attribute,
        which has a "metadata" attribute.

        example for "kg_rel_map":
        kg_rel_map = {
            'Harry': [['DREAMED_OF', 'Unknown relation'], ['FELL_HARD_ON', 'Concrete floor']],
            'Potter': [['WORE', 'Round glasses'], ['HAD', 'Dream']]
        }

        Args:
            retrieve (list[NodeWithScore]): list of NodeWithScore objects
        return:
            list[str]: list of string entities
        """

        entities = []
        kg_rel_map = retrieve[0].node.metadata["kg_rel_map"]
        for key, items in kg_rel_map.items():
            # key is the entity of question
            entities.append(key)
            # items is a list of [relationship, entity]
            entities.extend(item[1] for item in items)
            if len(entities) > MAX_ENTITIES_FROM_KG:
                break
        entities = list(set(entities))
        for exceptions in ENTITY_EXCEPTIONS:
            if exceptions in entities:
                entities.remove(exceptions)
        return entities
