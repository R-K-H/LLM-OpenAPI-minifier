import json
import os
import tiktoken
from collections import defaultdict
import string
import re
import shutil
from textwrap import dedent

tokenizer = tiktoken.encoding_for_model("text-embedding-ada-002")

output_directory = 'minified_openapi_docs'

# input_filepath = 'tatum_swagger.json'
# api_url_format = 'https://apidoc.tatum.io/tag/{tag}#operation/{operationId}'

input_filepath = 'stackpath_edge_compute_swagger.json'
api_url_format = 'https://stackpath.dev/reference/{operationId}'

# input_filepath = 'weather_dot_gov_swagger.json'
# api_url_format = 'https://www.weather.gov/documentation/services-web-api#/default/{operationId}'

# By default it creates a document for each endpoint

# Create "balanced chunks" of documents consisting of multiple endpoints around the size of token_count_goal
balanced_chunks = False # Currently disabled
token_count_goal = 3000

# Max token count for both document styles
token_count_max = 4500

# for any nested fields move them from the nested structure to the root aka flatten
# Decide what fields you want to keep in the documents
keys_to_keep = { 
    # Root level keys to populate
    "parameters": True,
    "good_responses": True, 
    "bad_responses": False,
    "request_bodies": True, 
    "schemas": True,
    "endpoint_descriptions": True,
    "endpoint_summaries": True, 
    # Keys to exclude
    "enums": True,
    "nested_descriptions": False, 
    "examples": False, 
    "tag_descriptions": False,
    "deprecated": False,
}

methods_to_handle = {"get", "post", "patch", "delete"}

# Saves tokens be abbreviating in a way understood by the LLM
# Must be lowercase
key_abbreviations = {
    "operationid": "opid",
    "parameters": "params",
    "requestbody": "reqBody",
    "properties": "props",
    "schemaname": "schName",
    "description": "desc",
    "summary": "sum",
    "string": "str",
    "number": "num",
    "object": "obj",
    "boolean": "bool",
    "array": "arr",
    "object": "obj"
}

def main():
    
    # Load JSON file into a Python dictionary
    with open(input_filepath) as f:
        openapi_spec = json.load(f)

    # Create list of processed and parsed individual endpoints
    endpoints_by_tag, endpoints_by_tag_metadata, server_url, tag_summary_dict = write_endpoints(openapi_spec)

    # if balanced_chunks == True:
    #     print("currently disabled")
    #     continue
    #     # Combine endpoints in groups of tags of relatively the same size token count
    #     docs = create_balanced_chunks(endpoints_by_tag, server_url)
    #     # Rewrite to so there is only one version of this function
    #     create_key_point_guide_for_chunks(docs, tag_summary_dict)
    #     count_tokens_in_directory(f'{output_directory}/balanced_chunks')
    # else:
    # default case
    endpoints_by_tag_metadata = create_endpoint_files(endpoints_by_tag_metadata)
    create_key_point_guide(endpoints_by_tag_metadata, tag_summary_dict)
    count_tokens_in_directory(f'{output_directory}')
    # Create LLM OAS keypoint generator guide file 
    # Need to add summaries 
    
def write_endpoints(openapi_spec):
    
    server_url = openapi_spec['servers'][0]['url']  # Fetch the server URL from the openapi_spec specification
    
    # If the tags key + description doesn't exist at the root of the spec, tags will be added from the endpoints
    tag_summary_dict = {}
    tags = openapi_spec.get('tags')
    if tags:
        # Iterate through the list of tags
        for tag in tags:
            # Extract name and description
            name = tag.get("name")
            description = tag.get("description")
            # Add to the dictionary
            if name and description:
                tag_summary_dict[name] = description.lower()

    # Dictionary with each unique tag as a key, and the value is a list of finalized endpoints with that tag
    endpoints_by_tag = defaultdict(list)
    endpoints_by_tag_metadata = defaultdict(list)
    endpoint_counter = 0
    for path, methods in openapi_spec['paths'].items():
        for method, endpoint in methods.items():
            if method not in methods_to_handle:
                continue
            if endpoint.get('deprecated', False) and not keys_to_keep["deprecated"]:
                continue
            endpoint_counter += 1
            
            # Adds schema to each endpoint
            if keys_to_keep["schemas"]:
                extracted_endpoint_data = resolve_refs(openapi_spec, endpoint)
            else:
                extracted_endpoint_data = endpoint
            
            # Populate output list with desired keys
            extracted_endpoint_data = populate_keys(extracted_endpoint_data, path)

            # If key == None or key == ''
            extracted_endpoint_data = remove_empty_keys(extracted_endpoint_data)

            # Remove unwanted keys
            extracted_endpoint_data = remove_unnecessary_keys(extracted_endpoint_data)

            # Flattens to remove nested objects where the dict has only one key
            extracted_endpoint_data = flatten_endpoint(extracted_endpoint_data)

            # Replace common keys with abbreviations and sets all text to lower case
            extracted_endpoint_data = minify(extracted_endpoint_data, key_abbreviations)
            
            # Get the tags of the current endpoint
            tags = endpoint.get('tags', [])
            tags = [tag for tag in tags]
            if not tags:
                tag = 'default'
            # For each tag, add the finalized endpoint to the corresponding list in the dictionary
            for tag in tags:
                endpoints_by_tag[tag].append(extracted_endpoint_data)

            operation_id = endpoint.get('operationId', '').lower()

            api_url = api_url_format.format(tag=tag, operationId=operation_id)

            context_string = write_dict_to_text(extracted_endpoint_data)
            metadata = {
                'tag': tag,
                'tag_number': 0,
                'doc_number': 0,
                'operation_id': operation_id,
                'doc_url': api_url,
                'server_url': f'{server_url}{path}'
            }
            endpoint_dict = {
                "metadata": metadata,
                "context": context_string
            }

            endpoints_by_tag_metadata[tag].append(endpoint_dict)

    # Sort alphabetically by tag name
    sorted_items = sorted(endpoints_by_tag.items())
    endpoints_by_tag = defaultdict(list, sorted_items)
    # Sort alphabetically by tag name
    sorted_items = sorted(endpoints_by_tag_metadata.items())
    endpoints_by_tag_metadata = defaultdict(list, sorted_items)
    
    # In the case tag_summary_dict is empty or missing tags this adds them here
    for tag in endpoints_by_tag.keys():
        # If the tag is not already in tag_summary_dict, add it with an empty description
        if tag not in tag_summary_dict:
            tag_summary_dict[tag] = ""

    print(f'{endpoint_counter} endpoints found')
    return endpoints_by_tag, endpoints_by_tag_metadata, server_url, tag_summary_dict
            
def resolve_refs(openapi_spec, endpoint):
    if isinstance(endpoint, dict):
        new_endpoint = {}
        for key, value in endpoint.items():
            if key == '$ref':
                ref_path = value.split('/')[1:]
                ref_object = openapi_spec
                for p in ref_path:
                    ref_object = ref_object.get(p, {})
                
                # Recursively resolve references inside the ref_object
                ref_object = resolve_refs(openapi_spec, ref_object)

                # Use the last part of the reference path as key
                new_key = ref_path[-1]
                new_endpoint[new_key] = ref_object
            else:
                # Recursively search in nested dictionaries
                new_endpoint[key] = resolve_refs(openapi_spec, value)
        return new_endpoint

    elif isinstance(endpoint, list):
        # Recursively search in lists
        return [resolve_refs(openapi_spec, item) for item in endpoint]

    else:
        # Base case: return the endpoint as is if it's neither a dictionary nor a list
        return endpoint

def populate_keys(endpoint, path):
    # Gets the main keys from the specs
    extracted_endpoint_data = {}
    extracted_endpoint_data['path'] = path
    extracted_endpoint_data['operationId'] = endpoint.get('operationId')

    if keys_to_keep["parameters"]:
            extracted_endpoint_data['parameters'] = endpoint.get('parameters')

    if keys_to_keep["endpoint_summaries"]:
            extracted_endpoint_data['summary'] = endpoint.get('summary')

    if keys_to_keep["endpoint_descriptions"]:
            extracted_endpoint_data['description'] = endpoint.get('description')

    if keys_to_keep["request_bodies"]:
            extracted_endpoint_data['requestBody'] = endpoint.get('requestBody')

    if keys_to_keep["good_responses"] or keys_to_keep["bad_responses"]:
        extracted_endpoint_data['responses'] = {}

    if keys_to_keep["good_responses"]:
        if 'responses' in endpoint and '200' in endpoint['responses']:
            extracted_endpoint_data['responses']['200'] = endpoint['responses'].get('200')

    if keys_to_keep["bad_responses"]:
        if 'responses' in endpoint:
            # Loop through all the responses
            for status_code, response in endpoint['responses'].items():
                # Check if status_code starts with '4' or '5' (4xx or 5xx)
                if status_code.startswith('4') or status_code.startswith('5') or 'default' in status_code:
                    # Extract the schema or other relevant information from the response
                    bad_response_content = response
                    if bad_response_content is not None:
                        extracted_endpoint_data['responses'][f'{status_code}'] = bad_response_content
    
    return extracted_endpoint_data

def remove_empty_keys(endpoint):
    if isinstance(endpoint, dict):
        # Create a new dictionary without empty keys
        new_endpoint = {}
        for key, value in endpoint.items():
            if value is not None and value != '':
                # Recursively call the function for nested dictionaries
                cleaned_value = remove_empty_keys(value)
                new_endpoint[key] = cleaned_value
        return new_endpoint
    elif isinstance(endpoint, list):
        # Recursively call the function for elements in a list
        return [remove_empty_keys(item) for item in endpoint]
    else:
        # Return the endpoint if it's not a dictionary or a list
        return endpoint

def remove_unnecessary_keys(endpoint):

    # Stack for storing references to nested dictionaries/lists and their parent keys
    stack = [(endpoint, [])]

    # Continue until there is no more data to process
    while stack:
        current_data, parent_keys = stack.pop()

        # If current_data is a dictionary
        if isinstance(current_data, dict):
            # Iterate over a copy of the keys, as we may modify the dictionary during iteration
            for k in list(current_data.keys()):
                # Check if this key should be removed based on settings and context
                if k == 'example' and not keys_to_keep["examples"]:
                    del current_data[k]
                if k == 'enum' and not keys_to_keep["enums"]:
                    del current_data[k]
                elif k == 'description' and len(parent_keys) > 0 and not keys_to_keep["nested_descriptions"]:
                    del current_data[k]
                # Otherwise, if the value is a dictionary or a list, add it to the stack for further processing
                # Check if the key still exists before accessing it
                if k in current_data and isinstance(current_data[k], (dict, list)):
                    stack.append((current_data[k], parent_keys + [k]))

        # If current_data is a list
        elif isinstance(current_data, list):
            # Add each item to the stack for further processing
            for item in current_data:
                if isinstance(item, (dict, list)):
                    stack.append((item, parent_keys + ['list']))
        
    return endpoint

def flatten_endpoint(endpoint):
    if not isinstance(endpoint, dict):
        return endpoint

    flattened_endpoint = {}

    # Define the set of keys to keep without unwrapping
    keep_keys = {"responses", "default", "200"}
    
    for key, value in endpoint.items():
        if isinstance(value, dict):
            # Check if the dictionary has any of the keys that need to be kept
            if key in keep_keys or (isinstance(key, str) and (key.startswith('5') or key.startswith('4'))):
                # Keep the inner dictionaries but under the current key
                flattened_endpoint[key] = flatten_endpoint(value)
            else:
                # Keep unwrapping single-key dictionaries
                while isinstance(value, dict) and len(value) == 1:
                    key, value = next(iter(value.items()))
                # Recursively flatten the resulting value
                flattened_endpoint[key] = flatten_endpoint(value)
        else:
            # If the value is not a dictionary, keep it as is
            flattened_endpoint[key] = value

    return flattened_endpoint

def minify(data, abbreviations):
    if isinstance(data, dict):
        # Lowercase keys, apply abbreviations and recursively process values
        return {
            abbreviations.get(key.lower(), key.lower()): minify(abbreviations.get(str(value).lower(), value), abbreviations)
            for key, value in data.items()
        }
    elif isinstance(data, list):
        # Recursively process list items
        return [minify(item, abbreviations) for item in data]
    elif isinstance(data, str):
        # If the data is a string, convert it to lowercase and replace if abbreviation exists
        return abbreviations.get(data.lower(), data.lower())
    else:
        # Return data unchanged if it's not a dict, list or string
        return data

def create_endpoint_files(endpoints_by_tag_metadata):

    # If output_directory exists, delete it.
    root_output_directory = os.path.join(output_directory)
    if os.path.exists(root_output_directory):
        shutil.rmtree(root_output_directory)

    # Initialize tag and operationId counters
    tag_counter = 0
    

    # Now, iterate over each unique tag
    for tag, endpoints_with_tag in endpoints_by_tag_metadata.items():
        endpoint_counter = 0
        # Create a subdirectory for the tag
        tag_directory = os.path.join(output_directory, tag)
        os.makedirs(tag_directory, exist_ok=True)

        for endpoint in endpoints_with_tag:
            endpoint['metadata']['tag_number'] = tag_counter
            endpoint['metadata']['doc_number'] = endpoint_counter
   
            # Create a file name 
            file_name = f"{tag_counter}-{endpoint_counter}.json"
            # Define the file path
            file_path = os.path.join(tag_directory, file_name)

            # Write the data to a JSON file
            with open(file_path, 'w') as file:
                json.dump(endpoint, file)

            endpoint_counter += 1

        tag_counter += 1

    return endpoints_by_tag_metadata

# If balanced_chunks is True
def create_balanced_chunks(endpoints_by_tag, server_url):
    # If output_directory exists, delete it.
    root_output_directory = os.path.join(output_directory)
    if os.path.exists(root_output_directory):
        shutil.rmtree(root_output_directory)

    # Create a subdirectory called 'endpoints' within the output directory
    endpoints_directory = os.path.join(output_directory, 'balanced_chunks')
    os.makedirs(endpoints_directory, exist_ok=True)

    # Initialize tag and operationId counters
    tag_counter = 0
    docid_counter = 0

    docs = []

    endpoint_counter = 0
    # Now, iterate over each unique tag
    for tag, endpoints_with_tag in endpoints_by_tag.items():

        endpoint_combos = distribute_endpoints(endpoints=endpoints_with_tag, tag=tag, goal_length=token_count_goal)
        for combo in endpoint_combos:
            # Creating a dictionary to hold the information of the combo.
            doc = {"endpoints": []}
            doc_context_string = ''
            for endpoint in combo:
                endpoint_counter += 1
                # Adding each endpoint to the doc
                doc["endpoints"].append(endpoint)
            
                formatted_text = write_dict_to_text(endpoint)
                doc_context_string += f'{formatted_text}\n'

            doc_context_token_count = tiktoken_len(doc_context_string)

            metadata = {
                'tag': tag,
                'tag_number': tag_counter,
                'doc_number': docid_counter,
                'doc_url': f"{api_docs_base_url}{tag}",
                'server_url': server_url,
                'token_count': doc_context_token_count
            }
            doc['metadata'] = metadata

            json_output = {
                "metadata": metadata,
                "doc_context": doc_context_string
            }

            # Create a file name 
            file_name = f"{tag_counter}-{tag}-{docid_counter}-{doc_context_token_count}.json"
            # Define the file path
            file_path = os.path.join(endpoints_directory, file_name)

            # Write the data to a JSON file
            with open(file_path, 'w') as file:
                json.dump(json_output, file)

            docs.append(doc)
            docid_counter += 1
        tag_counter += 1
    print(f'{endpoint_counter} endpoints added to docs')
    return docs

# Called by create_balanced_chunks to create chunks near token_count_goal
def distribute_endpoints(endpoints, tag, goal_length, depth=0):
    # Build initial combos
    combos = []
    current_combo = []
    combo_token_count = 0
    for index, endpoint in enumerate(endpoints):
        endpoint_token_count= tiktoken_len(write_dict_to_text(endpoint))
        # If too big, truncate operationid
        if endpoint_token_count > token_count_max:
            print(f'truncating: {endpoint["opid"]}\n token count: {endpoint_token_count}')
            operation_id_url = f'endpoint spec too long. see {api_docs_base_url}{tag}/#operation/{endpoint["opid"]} for more info.'
            truncated_endpoint = {
                'path': endpoint['path'],
                'opid': endpoint['opid'],
                'sum': endpoint.get('sum', ''),
                'message': operation_id_url
            }
            endpoints[index] = truncated_endpoint
            endpoint = truncated_endpoint
            endpoint_token_count = tiktoken_len(write_dict_to_text(endpoint))
        if goal_length > (combo_token_count + endpoint_token_count):
            current_combo.append(endpoint)
            combo_token_count += endpoint_token_count
            continue
        # Past here we're creating new combo
        if not current_combo:
            # If current empty add endpoint to current, current to combos, and empty current
            current_combo.append(endpoint)
            combos.append(current_combo)
            current_combo = []
            combo_token_count = 0
        else:
            # If current combo exists append current combo to combos, clear current, and append endpoint to current
            combos.append(current_combo)
            current_combo = []
            current_combo.append(endpoint)
            combo_token_count = endpoint_token_count
 
    # Catch last combo
    if current_combo:
        combos.append(current_combo)
    
    if depth >= 4:
        # Return the combos as is, if maximum recursion depth is reached
        return combos
    
    if len(combos) < 2:
        return combos
    
    # Check if any individual combo's token count is below 65% of the goal_length
    for combo in combos:
        combo_token_counts = [tiktoken_len(write_dict_to_text(endpoint)) for endpoint in combo]
        combo_token_count = sum(combo_token_counts)
        if combo_token_count < goal_length * 0.75:
            if goal_length > token_count_max:
                return combos
            # Increase the goal length by distributing the token count of the first undersized combo
            new_goal_length = goal_length + (combo_token_count / (len(combos) - 1))
            return distribute_endpoints(endpoints=endpoints, tag=tag, goal_length=new_goal_length, depth=depth + 1)

    return combos

def write_dict_to_text(data):
    def remove_html_tags_and_punctuation(input_str):
        # Strip HTML tags
        no_html_str = re.sub('<.*?>', '', input_str)
        # Define the characters that should be considered as punctuation
        modified_punctuation = set(string.punctuation) - {'/', '#'}
        # Remove punctuation characters
        return ''.join(ch for ch in no_html_str if ch not in modified_punctuation).strip()
    
    # List to accumulate the formatted text parts
    formatted_text_parts = []
    
    # Check if data is a dictionary
    if isinstance(data, dict):
        # Iterate over items in the dictionary
        for key, value in data.items():
            # Remove HTML tags and punctuation from key
            key = remove_html_tags_and_punctuation(key)
            
            # Depending on the data type, write the content
            if isinstance(value, (dict, list)):
                # Append the key followed by its sub-elements
                formatted_text_parts.append(key)
                formatted_text_parts.append(write_dict_to_text(value))
            else:
                # Remove HTML tags and punctuation from value
                value = remove_html_tags_and_punctuation(str(value))
                # Append the key-value pair
                formatted_text_parts.append(f"{key} {value}")
    # Check if data is a list
    elif isinstance(data, list):
        # Append each element in the list
        for item in data:
            formatted_text_parts.append(write_dict_to_text(item))
    # If data is a string or other type
    else:
        # Remove HTML tags and punctuation from data
        data = remove_html_tags_and_punctuation(str(data))
        # Append the data directly
        formatted_text_parts.append(data)
    
    # Join the formatted text parts with a single newline character
    # but filter out any empty strings before joining
    return '\n'.join(filter(lambda x: x.strip(), formatted_text_parts))

def create_key_point_guide(endpoints_by_tag_metadata, tag_summary_dict):
    # Ensure output directory exists
    os.makedirs(output_directory, exist_ok=True)
    # Define output file path
    output_file_path = os.path.join(output_directory, 'LLM_OAS_keypoint_guide_file.txt')

    output_string = dedent('''\
    dear agent,
    the user has a query that can be answered with an openapi spec document
    please use this llm parsable index of openapi spec documentation in the format:
    {{tag_number}}{{tag}} {{tag_description}}
    {{operationId}}{{doc_number}}{{operationId}}{{doc_number}}...
    {{tag_number}}{{tag}}
    ...

    each operationId in has an associated doc_number 
    using this index please return the most relevant operationIds
    do so STRICTLY by specifying in the following format 
    IMPORTANTLY REPLY ONLY with numbers and \\n characters:

    {{tag_number}}
    {{doc_number}}
    {{doc_number}}
    ...
    \\n
    {{tag_number}}
    ...
    thank you agent,
    begin

    ''')

    # Now, iterate over each unique tag
    for tag, endpoints_with_tag in endpoints_by_tag_metadata.items():
        tag_number = endpoints_with_tag[0].get('metadata', {}).get('tag_number', '')

        # If we're adding tag descriptions and they exist they're added here.
        tag_description = tag_summary_dict.get(tag)
        if keys_to_keep["tag_descriptions"] and tag_description is not None and tag_description != '':
            tag_description = tag_summary_dict.get(tag)
            tag_description = write_dict_to_text(tag_description)
            tag_string = f'{tag_number}{tag} {tag_description}\n'
        else:
            tag_string = f'{tag_number}{tag}\n'

        for endpoint in endpoints_with_tag:
            # tagtag_number-description\noperation_iddoc_numberoperation_iddoc_number\n
            metadata = endpoint.get('metadata', '')
            doc_number = metadata.get('doc_number', '')
            operation_id = metadata.get('operation_id', '')

            tag_string += f'{operation_id}{doc_number}'

        output_string += f'{tag_string}\n'

    print(f'keypoint file token count: {tiktoken_len(output_string)}')
    # Write sorted info_strings to the output file
    with open(output_file_path, 'w') as output_file:
            output_file.write(output_string)

# Rewrite to so there is only one version of this function
def create_key_point_guide_for_chunks(docs, tag_summary_dict):

    # Ensure output directory exists
    os.makedirs(output_directory, exist_ok=True)
    # Define output file path
    output_file_path = os.path.join(output_directory, 'LLM_OAS_keypoint_guide_file.txt')

    # List to hold the info_strings
    docs_by_tag = {}
    for doc in docs:
        tag = doc.get('metadata').get('tag')
        if tag:
            if tag not in docs_by_tag:
                docs_by_tag[tag] = []  # Initialize list for this tag
            docs_by_tag[tag].append(doc)
    
    output_string = ''
    for tag, tag_docs in docs_by_tag.items():
            # If we're adding tag descriptions and they exist they're added here.
            if keys_to_keep["tag_descriptions"]:
                tag_description = tag_summary_dict.get(tag)
                if tag_description is not None and tag_description != '':
                    tag_description = write_dict_to_text(tag_description)
                    tag_string = f'{tag}-{tag_description}\n'
                else:
                    tag_string = f'{tag}\n'
            else:
                tag_string = f'{tag}\n'
            for doc in tag_docs:
                # Extract the required information from the YAML file
                metadata = doc.get('metadata', '')
                doc_number = metadata.get('doc_number', '')
                endpoints = doc.get('endpoints', [])
                doc_string = f'{doc_number}'
                operation_id_counter = 0
                for endpoint in endpoints:
                    op_id = endpoint.get('opid', '')
                    doc_string += f'{op_id}{operation_id_counter}'
                    operation_id_counter += 1
                tag_string += f'{doc_string}\n'
            output_string += f'{tag_string}'

    print(f'keypoint file token count: {tiktoken_len(output_string)}')
    # Write sorted info_strings to the output file
    with open(output_file_path, 'w') as output_file:
            output_file.write(output_string)

def count_tokens_in_directory(directory):
    token_counts = []
    max_tokens = 0
    max_file = ''
    
    for dirpath, dirnames, filenames in os.walk(directory):
        for filename in filenames:
            if filename.endswith('.json'):
                filepath = os.path.join(dirpath, filename)
                with open(filepath, 'r') as file:
                    file_content = json.load(file)
                    context_content = file_content.get("context", "")
                    token_count = tiktoken_len(context_content)
                    token_counts.append(token_count)
                    if token_count > max_tokens:
                        max_tokens = token_count
                        max_file = filepath

    print("Total files:", len(token_counts))
    if not token_counts:
        return
    print("Min:", min(token_counts))
    print("Avg:", int(sum(token_counts) / len(token_counts)))
    print("Max:", max_tokens, "File:", max_file)
    print("Total tokens:", int(sum(token_counts)))

    return token_counts

def tiktoken_len(text):
    tokens = tokenizer.encode(
        text,
        disallowed_special=()
    )
    return len(tokens)

main()

    
