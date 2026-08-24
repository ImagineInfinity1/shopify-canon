# Shopify Custom App Setup Guide for GraphQL Access

## Issue: Current Custom App lacks GraphQL Admin API access

Your current Custom App token works for REST API but returns 404 for GraphQL endpoints. This means the app was created without GraphQL Admin API capabilities.

## Solution: Create New Custom App

### Step 1: Create New Custom App
1. Go to Shopify Admin
2. Settings → Apps and sales channels → Develop apps
3. Click "Create an app"
4. Name: "AI Bulk Uploader v2" (or similar)
5. Click "Create app"

### Step 2: Configure Admin API Access
1. Click "Configure Admin API scopes"
2. Enable these scopes:
   - ✅ `read_products`
   - ✅ `write_products`
   - ✅ `read_product_listings`
   - ✅ `write_product_listings`
3. Click "Save"

### Step 3: Install & Generate Token
1. Click "Install app"
2. Go to "API credentials" tab
3. Under "Admin API access token" click "Reveal token once"
4. Copy the token (starts with `shpat_`)

### Step 4: Update Environment
Replace the old token with the new one in Replit Secrets.

### Step 5: Test
The system will automatically test GraphQL access with the new token.

## Why This Happens
- Shopify's Custom Apps created before certain updates may lack GraphQL access
- Only way to add GraphQL capability is to create a new Custom App
- REST API continues to work with old tokens, but GraphQL requires newer app configuration

## Current System Status
✅ AI metadata generation working perfectly
✅ Image processing pipeline operational  
✅ UI with manual overrides functional
❌ Shopify GraphQL integration blocked (final step)

Once new Custom App is created with GraphQL access, the system will be fully operational.