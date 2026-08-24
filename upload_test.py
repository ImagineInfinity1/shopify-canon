#!/usr/bin/env python3
"""
Test script to verify PSD upload functionality
"""

import requests
import os

def test_psd_upload():
    # URL for your local Flask app
    url = "http://localhost:5000/upload_psd_template"
    
    # Create a small test file to simulate PSD upload
    test_file_content = b"PSD test content - this would be a real PSD file"
    
    files = {'files': ('test.psd', test_file_content, 'application/octet-stream')}
    
    try:
        response = requests.post(url, files=files)
        print(f"Status Code: {response.status_code}")
        print(f"Response: {response.text}")
        
        if response.status_code == 200:
            print("✅ Upload successful!")
        else:
            print("❌ Upload failed")
            
    except Exception as e:
        print(f"❌ Error: {e}")

if __name__ == "__main__":
    test_psd_upload()