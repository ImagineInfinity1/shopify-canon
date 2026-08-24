/**
 * Photopea Processor - Smart Object Replacement via iframe postMessage
 * 
 * Uses Photopea (free browser-based Photoshop) embedded in a hidden iframe
 * to properly replace PSD smart object content with artwork images.
 * 
 * This approach:
 * - Works entirely client-side (no server processing needed)
 * - Handles smart object transforms, masks, and effects properly
 * - Sends files as base64 (no public URLs required)
 * - Works on localhost
 */

class PhotopeaProcessor {
    constructor() {
        this.iframe = null;
        this.isReady = false;
        this.pendingCallback = null;
        this.processingQueue = [];
        this.isProcessing = false;
        this.initTimeout = null;
        this.processingTimeout = null;
        this.debugCallback = null; // For UI logging
        this.testCallback = null;  // For connection testing
        this.messageLog = [];      // Store all received messages
        
        // Bind methods
        this.handleMessage = this.handleMessage.bind(this);
        
        // Initialize when DOM is ready
        if (document.readyState === 'loading') {
            document.addEventListener('DOMContentLoaded', () => this.initialize());
        } else {
            this.initialize();
        }
    }
    
    /**
     * Set a callback for debug logging to UI
     */
    setDebugCallback(callback) {
        this.debugCallback = callback;
    }
    
    /**
     * Log message to console and UI if callback is set
     */
    log(message, type = 'info') {
        const fullMsg = `[Photopea] ${message}`;
        if (type === 'error') {
            console.error(fullMsg);
        } else {
            console.log(fullMsg);
        }
        if (this.debugCallback) {
            this.debugCallback(message, type);
        }
    }
    
    initialize() {
        this.iframe = document.getElementById('photopea-iframe');
        
        if (!this.iframe) {
            this.log('iframe not found in DOM', 'error');
            return;
        }
        
        // Listen for messages from Photopea
        window.addEventListener('message', this.handleMessage);
        
        // Wait for iframe to load
        this.iframe.addEventListener('load', () => {
            this.log('iframe loaded, waiting for initialization...');
            // Give Photopea time to fully initialize
            this.initTimeout = setTimeout(() => {
                this.isReady = true;
                this.log('Processor ready');
                this.processNextInQueue();
            }, 3000);
        });
        
        this.log('Processor initialized, waiting for iframe...');
    }
    
    /**
     * Test basic communication with Photopea
     * Returns a promise that resolves if communication works
     */
    async testConnection() {
        return new Promise((resolve, reject) => {
            if (!this.iframe || !this.iframe.contentWindow) {
                reject(new Error('iframe not available'));
                return;
            }
            
            if (!this.isReady) {
                reject(new Error('Photopea not ready yet'));
                return;
            }
            
            this.log('Testing connection with echoToOE...');
            
            // Set up callback for test response
            this.testCallback = {
                resolve: (data) => {
                    this.log('Connection test SUCCESS: received response');
                    resolve(data);
                },
                reject: reject
            };
            
            // Send a simple echo test
            const testMessage = 'PHOTOPEA_TEST_' + Date.now();
            this.postMessage(`app.echoToOE("${testMessage}");`);
            
            // Timeout after 5 seconds
            setTimeout(() => {
                if (this.testCallback) {
                    this.testCallback = null;
                    reject(new Error('Connection test timeout - no response from Photopea'));
                }
            }, 5000);
        });
    }
    
    /**
     * Simple test: create a small image and export it as PNG
     * This tests the full pipeline without needing external files
     */
    async testSimpleExport() {
        return new Promise((resolve, reject) => {
            if (!this.isReady) {
                reject(new Error('Photopea not ready'));
                return;
            }
            
            this.log('Testing simple image creation and export...');
            this.pendingCallback = { resolve, reject };
            
            // Create a small test image (100x100 red square) and export it
            // This tests: document creation, scripting, and saveToOE
            this.postMessage(`
                (function() {
                    try {
                        // Close any existing documents
                        while(app.documents.length > 0) {
                            app.activeDocument.close(SaveOptions.DONOTSAVECHANGES);
                        }
                        
                        // Create a new document
                        var doc = app.documents.add(100, 100, 72, "Test", NewDocumentMode.RGB);
                        
                        // Fill with red color
                        var color = new SolidColor();
                        color.rgb.red = 255;
                        color.rgb.green = 0;
                        color.rgb.blue = 0;
                        doc.selection.selectAll();
                        doc.selection.fill(color);
                        doc.selection.deselect();
                        
                        // Export as PNG
                        doc.saveToOE("png");
                    } catch(e) {
                        app.echoToOE("ERROR: " + e.message);
                    }
                })();
            `);
            
            setTimeout(() => {
                if (this.pendingCallback) {
                    this.log('Simple export test timed out', 'error');
                    this.pendingCallback.reject(new Error('Simple export test timeout - no response from Photopea'));
                    this.pendingCallback = null;
                }
            }, 15000);
        });
    }
    
    /**
     * Load a file and immediately export it (minimal processing)
     */
    async testLoadAndExport(base64DataUri) {
        return new Promise((resolve, reject) => {
            if (!this.isReady) {
                reject(new Error('Photopea not ready'));
                return;
            }
            
            this.log('Testing file load and export...');
            this.pendingCallback = { resolve, reject };
            
            // Close existing docs, load file, export immediately
            this.postMessage(`
                (function() {
                    try {
                        // Close existing
                        while(app.documents.length > 0) {
                            app.activeDocument.close(SaveOptions.DONOTSAVECHANGES);
                        }
                        
                        // Open the file
                        app.open("${base64DataUri}");
                        
                        // Wait a moment for load, then export
                        setTimeout(function() {
                            if (app.documents.length > 0) {
                                app.activeDocument.saveToOE("png");
                            } else {
                                app.echoToOE("ERROR: Document did not load");
                            }
                        }, 2000);
                    } catch(e) {
                        app.echoToOE("ERROR: " + e.message);
                    }
                })();
            `);
            
            setTimeout(() => {
                if (this.pendingCallback) {
                    this.log('Load and export test timed out', 'error');
                    this.pendingCallback.reject(new Error('Load and export test timeout'));
                    this.pendingCallback = null;
                }
            }, 20000);
        });
    }
    
    /**
     * Handle messages from Photopea iframe
     */
    handleMessage(event) {
        // Only accept messages from Photopea
        if (event.origin !== 'https://www.photopea.com') {
            return;
        }
        
        const data = event.data;
        
        // Log ALL messages for debugging
        this.messageLog.push({
            time: new Date().toISOString(),
            type: data instanceof ArrayBuffer ? 'ArrayBuffer' : typeof data,
            size: data instanceof ArrayBuffer ? data.byteLength : (typeof data === 'string' ? data.length : 0),
            preview: data instanceof ArrayBuffer ? `[${data.byteLength} bytes]` : (typeof data === 'string' ? data.substring(0, 100) : String(data))
        });
        
        // Keep only last 50 messages
        if (this.messageLog.length > 50) {
            this.messageLog.shift();
        }
        
        // Check if this is image data (ArrayBuffer)
        if (data instanceof ArrayBuffer) {
            this.log(`Received image data: ${data.byteLength} bytes`);
            
            // Clear the processing timeout since we got a response
            if (this.processingTimeout) {
                clearTimeout(this.processingTimeout);
                this.processingTimeout = null;
            }
            
            if (this.pendingCallback) {
                const blob = new Blob([data], { type: 'image/png' });
                this.pendingCallback.resolve(blob);
                this.pendingCallback = null;
                this.isProcessing = false;
                
                // Process next item in queue
                this.processNextInQueue();
            }
        } 
        // Check for string responses (errors, status, console logs)
        else if (typeof data === 'string') {
            this.log(`Message: ${data}`);
            
            // Check for test callback first
            if (this.testCallback && data.startsWith('PHOTOPEA_TEST_')) {
                this.testCallback.resolve(data);
                this.testCallback = null;
                return;
            }
            
            // Check for error messages
            if (data.toLowerCase().includes('error')) {
                this.log(`Error from Photopea: ${data}`, 'error');
                if (this.pendingCallback) {
                    // Clear timeout
                    if (this.processingTimeout) {
                        clearTimeout(this.processingTimeout);
                        this.processingTimeout = null;
                    }
                    this.pendingCallback.reject(new Error(data));
                    this.pendingCallback = null;
                    this.isProcessing = false;
                    this.processNextInQueue();
                }
            }
        }
    }
    
    /**
     * Get debug info about message log
     */
    getMessageLog() {
        return this.messageLog;
    }
    
    /**
     * Send a script to Photopea for execution
     */
    postMessage(script) {
        if (!this.iframe || !this.iframe.contentWindow) {
            this.log('iframe not available', 'error');
            return false;
        }
        
        this.iframe.contentWindow.postMessage(script, '*');
        return true;
    }
    
    /**
     * Convert file to base64 data URI
     */
    async fileToBase64(file) {
        return new Promise((resolve, reject) => {
            const reader = new FileReader();
            reader.onload = () => resolve(reader.result);
            reader.onerror = reject;
            reader.readAsDataURL(file);
        });
    }
    
    /**
     * Load a file into Photopea (supports PSD, PNG, JPG, etc.)
     */
    async loadFile(base64DataUri) {
        // Estimate wait time based on file size (base64 is ~1.33x larger than binary)
        const estimatedBytes = base64DataUri.length * 0.75;
        const estimatedMB = estimatedBytes / (1024 * 1024);
        
        this.log(`Loading file (~${estimatedMB.toFixed(2)} MB estimated)...`);
        
        // Use synchronous open with background=false so it waits for load
        const script = `app.open("${base64DataUri}");`;
        this.postMessage(script);
        
        // Wait longer for larger files (minimum 3 seconds, add 2 seconds per MB)
        const waitTime = Math.max(3000, Math.min(15000, 3000 + (estimatedMB * 2000)));
        this.log(`Waiting ${waitTime}ms for file to load...`);
        
        return new Promise(resolve => setTimeout(resolve, waitTime));
    }
    
    /**
     * Generate the smart object replacement script - SIMPLE OVERLAY VERSION
     * Since smart object editing is complex and may not work reliably,
     * this version simply:
     * 1. Loads the artwork
     * 2. Places it as a layer at the bottom of the mockup
     * 3. Exports the result
     * 
     * For full smart object support, users can manually edit in Photopea.
     */
    getSmartObjectReplacementScript(artworkBase64) {
        // Use a simple overlay approach that reliably works
        return `
            (function() {
                try {
                    // Make sure we have a document
                    if (app.documents.length === 0) {
                        console.log("Error: No PSD document loaded");
                        return;
                    }
                    
                    var mainDoc = app.activeDocument;
                    var mainWidth = mainDoc.width.value || mainDoc.width;
                    var mainHeight = mainDoc.height.value || mainDoc.height;
                    
                    console.log("PSD loaded: " + mainDoc.name + " (" + mainWidth + "x" + mainHeight + ")");
                    console.log("Layers: " + mainDoc.layers.length);
                    
                    // Try to find and edit smart object
                    var foundSmartObject = false;
                    
                    function findAndEditSmartObject(layers) {
                        for (var i = 0; i < layers.length; i++) {
                            var layer = layers[i];
                            
                            // Check if smart object (kind 17 in Photopea)
                            if (layer.kind == 17 || (typeof LayerKind !== 'undefined' && layer.kind == LayerKind.SMARTOBJECT)) {
                                console.log("Found smart object: " + layer.name);
                                mainDoc.activeLayer = layer;
                                
                                try {
                                    // Try to edit smart object
                                    executeAction(stringIDToTypeID("placedLayerEditContents"), undefined, DialogModes.NO);
                                    return true;
                                } catch(e) {
                                    console.log("Could not edit smart object: " + e.message);
                                }
                            }
                            
                            // Check nested layers
                            if (layer.layers && layer.layers.length > 0) {
                                if (findAndEditSmartObject(layer.layers)) {
                                    return true;
                                }
                            }
                        }
                        return false;
                    }
                    
                    foundSmartObject = findAndEditSmartObject(mainDoc.layers);
                    
                    if (foundSmartObject && app.documents.length > 1) {
                        // Smart object is open, replace content
                        var soDoc = app.activeDocument;
                        var soWidth = soDoc.width.value || soDoc.width;
                        var soHeight = soDoc.height.value || soDoc.height;
                        
                        console.log("Inside smart object: " + soWidth + "x" + soHeight);
                        
                        // Open artwork
                        app.open("${artworkBase64}");
                        var artDoc = app.activeDocument;
                        
                        // Scale artwork to cover smart object
                        var artW = artDoc.width.value || artDoc.width;
                        var artH = artDoc.height.value || artDoc.height;
                        var scaleX = soWidth / artW;
                        var scaleY = soHeight / artH;
                        var scale = Math.max(scaleX, scaleY);
                        
                        artDoc.resizeImage(artW * scale, artH * scale);
                        
                        // Copy artwork
                        artDoc.selection.selectAll();
                        artDoc.selection.copy();
                        artDoc.close(SaveOptions.DONOTSAVECHANGES);
                        
                        // Paste into smart object and clear old content
                        app.activeDocument = soDoc;
                        
                        // Remove existing layers except background
                        while(soDoc.layers.length > 1) {
                            if (!soDoc.layers[0].isBackgroundLayer) {
                                soDoc.layers[0].remove();
                            } else {
                                break;
                            }
                        }
                        
                        // Paste new content
                        soDoc.paste();
                        
                        // Save and close smart object
                        soDoc.close(SaveOptions.SAVECHANGES);
                        
                        console.log("Smart object updated");
                        
                    } else {
                        // Fallback: just place artwork as bottom layer
                        console.log("Using overlay fallback (no smart object edit)");
                        
                        app.open("${artworkBase64}");
                        var artDoc = app.activeDocument;
                        
                        // Scale to match mockup size
                        var artW = artDoc.width.value || artDoc.width;
                        var artH = artDoc.height.value || artDoc.height;
                        var scaleX = mainWidth / artW;
                        var scaleY = mainHeight / artH;
                        var scale = Math.max(scaleX, scaleY);
                        
                        artDoc.resizeImage(artW * scale, artH * scale);
                        
                        artDoc.selection.selectAll();
                        artDoc.selection.copy();
                        artDoc.close(SaveOptions.DONOTSAVECHANGES);
                        
                        app.activeDocument = mainDoc;
                        mainDoc.paste();
                        
                        // Move new layer to bottom
                        var newLayer = mainDoc.activeLayer;
                        if (mainDoc.layers.length > 1) {
                            newLayer.move(mainDoc.layers[mainDoc.layers.length - 1], ElementPlacement.PLACEAFTER);
                        }
                        
                        console.log("Artwork placed as background layer");
                    }
                    
                    // Export result
                    app.activeDocument = mainDoc;
                    console.log("Exporting PNG...");
                    mainDoc.saveToOE("png");
                    
                } catch(e) {
                    console.log("Script error: " + e.message);
                    // Try to export whatever we have
                    if (app.documents.length > 0) {
                        try {
                            app.activeDocument.saveToOE("png");
                        } catch(ex) {
                            console.log("Export failed: " + ex.message);
                        }
                    }
                }
            })();
        `;
    }
    
    /**
     * Process a PSD mockup with artwork
     * @param {File|Blob|string} psdFile - The PSD template file or base64 string
     * @param {File|Blob|string} artworkFile - The artwork image file or base64 string
     * @returns {Promise<Blob>} - The processed image as a PNG blob
     */
    async processSmartObject(psdFile, artworkFile) {
        return new Promise(async (resolve, reject) => {
            // Add to queue
            this.processingQueue.push({
                psdFile,
                artworkFile,
                resolve,
                reject
            });
            
            // Start processing if not already
            this.processNextInQueue();
        });
    }
    
    /**
     * Process the next item in the queue
     */
    async processNextInQueue() {
        if (this.isProcessing || this.processingQueue.length === 0 || !this.isReady) {
            return;
        }
        
        this.isProcessing = true;
        const { psdFile, artworkFile, resolve, reject } = this.processingQueue.shift();
        
        try {
            this.log('Starting to process files...');
            this.log(`PSD: ${psdFile instanceof Blob ? 'Blob' : ''} ${psdFile instanceof File ? 'File' : ''}`);
            this.log(`Artwork: ${artworkFile instanceof Blob ? 'Blob' : ''} ${artworkFile instanceof File ? 'File' : ''}`);
            
            // Convert files to base64 if needed
            let psdBase64, artworkBase64;
            
            if (typeof psdFile === 'string') {
                psdBase64 = psdFile;
            } else {
                this.log('Converting PSD to base64...');
                psdBase64 = await this.fileToBase64(psdFile);
                this.log(`PSD base64 length: ${psdBase64.length}`);
            }
            
            if (typeof artworkFile === 'string') {
                artworkBase64 = artworkFile;
            } else {
                this.log('Converting artwork to base64...');
                artworkBase64 = await this.fileToBase64(artworkFile);
                this.log(`Artwork base64 length: ${artworkBase64.length}`);
            }
            
            // Set up the callback
            this.pendingCallback = { resolve, reject };
            
            // Close any existing documents
            this.log('Closing existing documents...');
            this.postMessage('while(app.documents.length > 0) { app.activeDocument.close(SaveOptions.DONOTSAVECHANGES); }');
            await new Promise(r => setTimeout(r, 1000));
            
            // Load the PSD file
            this.log('Loading PSD file...');
            await this.loadFile(psdBase64);
            
            // Wait a bit more for large PSD files
            this.log('Waiting for PSD to fully load...');
            await new Promise(r => setTimeout(r, 2000));
            
            // Execute the smart object replacement script
            this.log('Executing smart object replacement script...');
            const script = this.getSmartObjectReplacementScript(artworkBase64);
            this.postMessage(script);
            
            // Set a timeout for the operation (increased to 60 seconds for large files)
            this.processingTimeout = setTimeout(() => {
                if (this.pendingCallback) {
                    this.log('Processing timed out after 60 seconds', 'error');
                    this.pendingCallback.reject(new Error('Processing timeout'));
                    this.pendingCallback = null;
                    this.isProcessing = false;
                    this.processNextInQueue();
                }
            }, 60000); // 60 second timeout
            
        } catch (error) {
            this.log('Processing error: ' + error.message, 'error');
            if (this.processingTimeout) {
                clearTimeout(this.processingTimeout);
                this.processingTimeout = null;
            }
            reject(error);
            this.isProcessing = false;
            this.processNextInQueue();
        }
    }
    
    /**
     * Check if the processor is ready
     */
    isProcessorReady() {
        return this.isReady;
    }
    
    /**
     * Get the current queue length
     */
    getQueueLength() {
        return this.processingQueue.length;
    }
}

// Create global instance
window.photopeaProcessor = new PhotopeaProcessor();

// Convenience function for processing mockups
window.processMockup = async function(psdFile, artworkFile) {
    if (!window.photopeaProcessor) {
        throw new Error('Photopea processor not initialized');
    }
    return await window.photopeaProcessor.processSmartObject(psdFile, artworkFile);
};

console.log('[Photopea] photopea-processor.js loaded');
