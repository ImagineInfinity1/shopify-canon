#!/usr/bin/env python3

# Quick script to clear processing queue and test the fix
import requests
import os

# Clear current processing queue
print("Clearing processing queue...")

# Get all current tasks
response = requests.get("http://localhost:5000/status")
if response.status_code == 200:
    data = response.json()
    tasks = data.get('tasks', [])
    print(f"Found {len(tasks)} tasks in queue")
    
    for task in tasks:
        task_id = task.get('id')
        if task_id:
            # Clear each task
            delete_response = requests.delete(f"http://localhost:5000/clear_task/{task_id}")
            if delete_response.status_code == 200:
                print(f"✓ Cleared task {task_id}")
            else:
                print(f"✗ Failed to clear task {task_id}")

print("\nQueue cleared. Ready to test fix!")
print("Now upload your green warrior helmet image again to test the fix.")