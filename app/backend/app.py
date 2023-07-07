import os
import mimetypes
import time
import logging
import openai
from flask import Flask, request, jsonify
from azure.identity import DefaultAzureCredential
from azure.search.documents import SearchClient
from approaches.retrievethenread import RetrieveThenReadApproach
from approaches.readretrieveread import ReadRetrieveReadApproach
from approaches.readdecomposeask import ReadDecomposeAsk
from approaches.chatreadretrieveread import ChatReadRetrieveReadApproach
from azure.storage.blob import BlobServiceClient

# Replace these with your own values, either in environment variables or directly here
AZURE_STORAGE_ACCOUNT = os.environ.get(
    "AZURE_STORAGE_ACCOUNT") or "mystorageaccount"
AZURE_STORAGE_CONTAINER = os.environ.get(
    "AZURE_STORAGE_CONTAINER") or "content"
AZURE_SEARCH_SERVICE = os.environ.get("AZURE_SEARCH_SERVICE") or "gptkb"
AZURE_SEARCH_INDEX = os.environ.get("AZURE_SEARCH_INDEX") or "gptkbindex"
AZURE_OPENAI_SERVICE = os.environ.get("AZURE_OPENAI_SERVICE") or "myopenai"
AZURE_OPENAI_GPT_DEPLOYMENT = os.environ.get(
    "AZURE_OPENAI_GPT_DEPLOYMENT") or "davinci"
AZURE_OPENAI_CHATGPT_DEPLOYMENT = os.environ.get(
    "AZURE_OPENAI_CHATGPT_DEPLOYMENT") or "chat"

KB_FIELDS_CONTENT = os.environ.get("KB_FIELDS_CONTENT") or "content"
KB_FIELDS_CATEGORY = os.environ.get("KB_FIELDS_CATEGORY") or "category"
KB_FIELDS_SOURCEPAGE = os.environ.get("KB_FIELDS_SOURCEPAGE") or "sourcepage"

# Use the current user identity to authenticate with Azure OpenAI, Cognitive Search and Blob Storage (no secrets needed,
# just use 'az login' locally, and managed identity when deployed on Azure). If you need to use keys, use separate AzureKeyCredential instances with the
# keys for each service
# If you encounter a blocking error during a DefaultAzureCredntial resolution, you can exclude the problematic credential by using a parameter (ex. exclude_shared_token_cache_credential=True)
azure_credential = DefaultAzureCredential()

# Used by the OpenAI SDK
openai.api_type = "azure"
openai.api_base = f"https://{AZURE_OPENAI_SERVICE}.openai.azure.com"
openai.api_version = "2022-12-01"

# Comment these two lines out if using keys, set your API key in the OPENAI_API_KEY environment variable instead
openai.api_type = "azure_ad"
openai_token = azure_credential.get_token(
    "https://cognitiveservices.azure.com/.default")
openai.api_key = openai_token.token

# Set up clients for Cognitive Search and Storage
search_client = SearchClient(
    endpoint=f"https://{AZURE_SEARCH_SERVICE}.search.windows.net",
    index_name=AZURE_SEARCH_INDEX,
    credential=azure_credential)
blob_client = BlobServiceClient(
    account_url=f"https://{AZURE_STORAGE_ACCOUNT}.blob.core.windows.net",
    credential=azure_credential)
blob_container = blob_client.get_container_client(AZURE_STORAGE_CONTAINER)

# Various approaches to integrate GPT and external knowledge, most applications will use a single one of these patterns
# or some derivative, here we include several for exploration purposes
ask_approaches = {
    "rtr": RetrieveThenReadApproach(search_client, AZURE_OPENAI_GPT_DEPLOYMENT, KB_FIELDS_SOURCEPAGE, KB_FIELDS_CONTENT),
    "rrr": ReadRetrieveReadApproach(search_client, AZURE_OPENAI_GPT_DEPLOYMENT, KB_FIELDS_SOURCEPAGE, KB_FIELDS_CONTENT),
    "rda": ReadDecomposeAsk(search_client, AZURE_OPENAI_GPT_DEPLOYMENT, KB_FIELDS_SOURCEPAGE, KB_FIELDS_CONTENT)
}

chat_approaches = {
    "rrr": ChatReadRetrieveReadApproach(search_client, AZURE_OPENAI_CHATGPT_DEPLOYMENT, AZURE_OPENAI_GPT_DEPLOYMENT, KB_FIELDS_SOURCEPAGE, KB_FIELDS_CONTENT)
}

app = Flask(__name__)


@app.route("/", defaults={"path": "index.html"})
@app.route("/<path:path>")
def static_file(path):
    return app.send_static_file(path)

# Serve content files from blob storage from within the app to keep the example self-contained.
# *** NOTE *** this assumes that the content files are public, or at least that all users of the app
# can access all the files. This is also slow and memory hungry.


@app.route("/content/<path>")
def content_file(path):
    blob = blob_container.get_blob_client(path).download_blob()
    mime_type = blob.properties["content_settings"]["content_type"]
    if mime_type == "application/octet-stream":
        mime_type = mimetypes.guess_type(path)[0] or "application/octet-stream"
    return blob.readall(), 200, {"Content-Type": mime_type, "Content-Disposition": f"inline; filename={path}"}


@app.route("/ask", methods=["POST"])
def ask():
    ensure_openai_token()
    approach = request.json["approach"]
    try:
        impl = ask_approaches.get(approach)
        if not impl:
            return jsonify({"error": "unknown approach"}), 400
        r = impl.run(request.json["question"],
                     request.json.get("overrides") or {})
        return jsonify(r)
    except Exception as e:
        logging.exception("Exception in /ask")
        return jsonify({"error": str(e)}), 500


@app.route("/chat", methods=["POST"])
def chat():
    ensure_openai_token()
    approach = request.json["approach"]
    try:
        impl = chat_approaches.get(approach)
        if not impl:
            return jsonify({"error": "unknown approach"}), 400
        r = impl.run(request.json["history"],
                     request.json.get("overrides") or {})
        return jsonify(r)
    except Exception as e:
        logging.exception("Exception in /chat")
        return jsonify({"error": str(e)}), 500


@app.route("/get_documents", methods=["POST"])
def get_document_names():
    blob_data = dict()
    for blob in blob_container.list_blobs():
        full_blob_name = blob.name
        last_hyphen_index = full_blob_name.rfind("-")
        base_name = full_blob_name[:last_hyphen_index].strip()

        # If the name is already in the dictionary, only overwrite if the new date is earlier.
        if base_name in blob_data and blob_data[base_name][0] < blob.last_modified:
            continue

        blob_data[base_name] = (
            blob.last_modified, blob.etag)  # Convert to tuple

    return list(blob_data.items())  # Convert to list of tuples


@app.route("/get_search", methods=["POST"])
def get_search():
    documents = []
    results = search_client.search(search_text="")

    for result in results:
        documents.append(result)

    return jsonify(documents)


@app.route("/delete_all_documents", methods=["POST"])
def delete_all_documents():
    print(f"Removing all documents from search index")

    while True:
        r = search_client.search("*", top=1000, include_total_count=True)

        if r.get_count() == 0:
            break

        r = search_client.delete_documents(
            documents=[{"id": d["id"]} for d in r])
        print(f"\tRemoved something from index")

        # It can take a few seconds for search results to reflect changes, so wait a bit
        time.sleep(2)
    return "Deleted all documents"


@app.route("/delete_document", methods=["POST"])
def delete_document():
    data = request.get_json()
    blob_name_to_delete = data.get('name')

    if not blob_name_to_delete:
        return jsonify({"error": "Missing 'name' parameter"}), 400

    blob_list = blob_container.list_blobs()
    for blob in blob_list:
        if blob.name.startswith(blob_name_to_delete):
            try:
                # Delete blob from storage
                blob_client_del = blob_client.get_blob_client(
                    "content", blob.name)
                blob_client_del.delete_blob()
                print(f"Blob {blob.name} has been deleted.")

            except Exception as e:
                print(f"Failed to delete blob: {blob.name}. Error: {e}")

    # Create filter for search
    print(blob_name_to_delete)
    filter = f"sourcefile eq '{blob_name_to_delete}.pdf'"

    while True:
        print("got inside the while loop")
        # Search for documents to delete
        r = search_client.search(
            search_text="*", filter=filter, top=1000, include_total_count=True)
        results = list(r)
        print(f"Count of results: {len(results)}")
        for result in results:
            print(f"Results: {result}")
        if len(results) == 0:
            break
        r = search_client.delete_documents(
            documents=[{"id": d["id"]} for d in results])
        print(f"\tRemoved {len(r)} sections from index")
        # It can take a few seconds for search results to reflect changes, so wait a bit
        time.sleep(2)

    return "200"


def ensure_openai_token():
    global openai_token
    if openai_token.expires_on < int(time.time()) - 60:
        openai_token = azure_credential.get_token(
            "https://cognitiveservices.azure.com/.default")
        openai.api_key = openai_token.token


if __name__ == "__main__":
    app.run()
