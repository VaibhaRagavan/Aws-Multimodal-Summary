import json
import boto3
import os
import time
import requests

rekognition = boto3.client('rekognition', region_name='eu-west-1')
model = boto3.client('bedrock-runtime', region_name='eu-west-1')
s3 = boto3.client('s3', region_name='eu-west-1')
transcribe = boto3.client('transcribe', region_name='eu-west-1')

#Detect the items in the image
def detect_labels(photo_bytes):
    return rekognition.detect_labels(
        Image={'Bytes': photo_bytes},
        MaxLabels=10,
        MinConfidence=80
    )

#Dtect the text in the image
def detect_text(photo_bytes):
    return rekognition.detect_text(
        Image={'Bytes': photo_bytes},
    )

#LLM model to get summary of the image
def img2text_model(label, text):
    modelid = os.environ["MODEL_ID"]
    prompt = {
        "messages": [
            {"role": "system",
             "content": "Describe images clearly and briefly using only the given labels and text. Rephrase into sentences but do not add new objects."},
            {"role": "user",
             "content": f"""
                    Labels: {label}
                    Text in image: {', '.join(text)}
                Instructions:
                 - Describe labels into a  sentence.
                 - Do not add extra information.
                Output format:
                    Only return the label description and text description, do not include reasoning and instruction
                    Labels description: <sentence>
                    Text description: <sentence>
               """}
        ]
    }

    bedrock_response = model.invoke_model(
        modelId=modelid,
        contentType="application/json",
        accept="application/json",
        body=json.dumps({"messages": prompt["messages"], "max_tokens_to_sample": 200})
    )
    result = json.loads(bedrock_response['body'].read())
    return result["choices"][0]["message"]["content"]


#LLM model to get the summary of the video
def video2text_model(content):
    modelid = os.environ["MODEL_ID"]
    prompt = {
        "messages": [
            {"role": "system",
             "content": "You are generating the summary from the vidoe transcribe"},
            {"role": "user",
             "content": f"""
                    content:{content}
                Instructions:
                Detect whether this transcript is from a:
                   1) Meeting-Summarize this meeting transcript into concise bullet points highlighting action items, decisions, and next steps.
                   2) Hospital review-Summarize the patient’s feedback into a structured review highlighting positives, complaints, and suggestions.
                   3) Online class-Summarize this lecture into short notes, highlighting main concepts, examples, and key takeaways.
                   4) Other-summarize the content 
                 - Do not add extra information.
                Output format:
                    Only return the summary, do not include reasoning or instructions
               """}
        ]
    }
    bedrock_result=model.invoke_model(
        modelId=modelid,
        contentType="application/json",
        accept="application/json",
        body=json.dumps({"messages": prompt["messages"], "max_tokens_to_sample": 200})
    )
    result = json.loads(bedrock_result['body'].read())
    return result["choices"][0]["message"]["content"]

#Transcribe convert audio/video to text 
def start_transcribe_job(bucket, key):
    job_name = f"transcribe-job-{int(time.time())}"
    media = f"s3://{bucket}/{key}"

    response = transcribe.start_transcription_job(
        TranscriptionJobName=job_name,
        Media={'MediaFileUri': media},
        MediaFormat=key.split('.')[-1],
        LanguageCode='en-US'
    )
    return response

#Get the converted transcribe_text 
def get_transcribe(job_name, max_wait_seconds=300, poll_interval=5):
    waited=0
    while waited<max_wait_seconds:
        result=transcribe.get_transcription_job(TranscriptionJobName=job_name)
        status=result["TranscriptionJob"]["TranscriptionJobStatus"]
        if status=="COMPLETED":
            uri=result["TranscriptionJob"]["Transcript"]["TranscriptFileUri"]
            r=requests.get(uri)
            transcription=r.json()
            transcribe_text=transcription['results']['transcripts'][0]['transcript']
            return transcribe_text
        elif status == 'FAILED':
            raise Exception("Transcription job failed")
        time.sleep(poll_interval)   
        waited += poll_interval
    raise TimeoutError("Transcription job did not complete in time")
#lambda fucntion
def lambda_handler(event, context):
    bucket_name = event['Records'][0]['s3']['bucket']['name']
    object_key = event['Records'][0]['s3']['object']['key']

    # Image processing
    if object_key.lower().endswith(('.jpg', '.jpeg', '.png')):
        s3_response = s3.get_object(Bucket=bucket_name, Key=object_key)
        image_bytes = s3_response['Body'].read()
        #image processing
        try:
            rekog_response = detect_labels(image_bytes)
            rekog_text = detect_text(image_bytes)
        except Exception as e:
            return {"statusCode": 500, "body": str(e)}

        labels = [item.get("Name", "") for item in rekog_response.get("Labels", [])]
        label_text = ', '.join(labels)
        texts = [item.get("DetectedText", "") for item in rekog_text.get("TextDetections", []) if item.get("Type") == "LINE"]
        summary = img2text_model(label_text, texts)
        print("Image Processed")
        
    # Audio/Video processing
    elif object_key.lower().endswith(('.mp3', '.wav', '.m4a','.mp4')):
        response = start_transcribe_job(bucket_name, object_key)['TranscriptionJob']['TranscriptionJobName']
        description =get_transcribe(response)
        print("Transcribe Generated")
        print(description)
        summary=video2text_model(description)
        labels=[]
        texts=[]


    else:
        return {"statusCode": 400, "body": "Enter a valid image or audio file format"}

    return {
        "statusCode": 200,
        "body": json.dumps({
            "LABELS": labels,
            "TEXT": texts,
            "DESCRIPTION":summary.encode('utf-8').decode('unicode_escape')
        })
    }