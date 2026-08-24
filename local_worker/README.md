# Listing Cannon Local PSD Worker

This keeps PSD rendering off Render. Your local machine does the heavy PhotoshopAPI work, then uploads only:

- the framed JPEG mockups, in PSD filename order
- a resized JPEG copy of the original raw artwork, used only for AI analysis

Render then uses the normal Ready Images Shopify listing pipeline.

## One-Time Render Setup

Add these environment variables to the Render service:

```text
LOCAL_WORKER_TOKEN=<make-a-long-random-password>
LOCAL_WORKER_USER_ID=<your Listing Cannon user id>
LOCAL_WORKER_PROFILE_NAME=MAIN 1
```

`LOCAL_WORKER_USER_ID` is the app user that owns the Shopify connection and saved profiles. In the current Render logs it appears as `80be3de4`.

Keep `GEMINI_API_KEY` and the Shopify connection configured as they already are.

## One-Time Local Setup

From the repo folder:

```powershell
python -m pip install -r requirements.txt
```

Copy `.env.example` to `.env` inside this `local_worker` folder and set:

```text
LOCAL_WORKER_TOKEN=<same token as Render>
PROFILE_NAME=MAIN 1
```

## Daily Use

1. Put PSD/PSB mockups in `local_worker/frames`.
2. Name the PSD files in the order you want Shopify images to appear, for example:
   - `01_BLACK_FRAME_MAIN.psd`
   - `02_DARK_WOOD_ANGLE.psd`
   - `03_TABLE_LIGHT.psd`
3. Every PSD must contain the Smart Object layer named `1`.
4. Double-click `Run Desktop App.bat`.
5. Drop raw artwork images into the desktop app, or click `Add Images`.

For each raw artwork, the worker creates one Shopify listing task with all framed mockups attached. It works from a local inbox copy and never moves or deletes the external source artwork.
By default it waits until Render reports the Shopify product as completed, then deletes its inbox/processing copy, generated mockups, and analysis/work files. Failed jobs are also cleaned because the source remains in its original folder and the desktop queue retains the error message.

If you want the listing to publish without stopping for manual review, make sure the saved Listing Cannon profile has review disabled, or leave `REVIEW_BEFORE_PUBLISH=false` in `.env`.

`Run Local Worker.bat` is still available for the old folder-watch mode, but the desktop app is the intended workflow.
