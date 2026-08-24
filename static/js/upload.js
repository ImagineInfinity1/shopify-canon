// Simple upload functions that work
function startSimpleUpload() {
    console.log('🔥 START SIMPLE UPLOAD CALLED!');
    const fileInput = document.getElementById('simple-file-input');
    if (fileInput) {
        console.log('✅ Found file input, clicking...');
        fileInput.click();
    } else {
        console.error('❌ File input not found!');
    }
}

// Make functions globally accessible
window.startSimpleUpload = startSimpleUpload;
window.updateVariantPreview = updateVariantPreview;
window.moveImageUp = moveImageUp;
window.moveImageDown = moveImageDown;
window.removeImage = removeImage;
window.clearAllImages = clearAllImages;
window.uploadAllImages = uploadAllImages;
window.collectVariantsData = collectVariantsData;

// Function to collect variant data from the page
function collectVariantsData() {
    console.log('🚀🚀🚀 VARIANT COLLECTION v2 - FIXED VERSION');
    console.log('🔍 DEBUG: Starting variant data collection...');
    const variants = [];
    
    // Try multiple container selectors
    let variantContainer = document.getElementById('variantsList');
    console.log('🔍 DEBUG: #variantsList found:', !!variantContainer);
    
    // Fallback to variantsContainer if variantsList not found
    if (!variantContainer) {
        variantContainer = document.getElementById('variantsContainer');
        console.log('🔍 DEBUG: #variantsContainer found:', !!variantContainer);
    }
    
    if (!variantContainer) {
        console.error('❌ CRITICAL: No variant container found! Trying document-wide search...');
    }
    
    // Try multiple selectors for variant rows
    let variantRows = document.querySelectorAll('#variantsList .variant-row');
    console.log('🔍 DEBUG: Found variant rows in #variantsList:', variantRows.length);
    
    // If no rows found, try broader selectors
    if (variantRows.length === 0) {
        variantRows = document.querySelectorAll('.variant-row');
        console.log('🔍 DEBUG: Found variant rows with broad selector:', variantRows.length);
    }
    
    // If still no rows, try within variantsContainer
    if (variantRows.length === 0 && variantContainer) {
        variantRows = variantContainer.querySelectorAll('.variant-row');
        console.log('🔍 DEBUG: Found variant rows in container:', variantRows.length);
    }
    
    // Log all input elements we can find for debugging
    if (variantRows.length === 0) {
        console.log('🔍 DEBUG: No .variant-row found. Searching for any variant inputs...');
        const allVariantInputs = document.querySelectorAll('.variant-name, .variant-price, .variant-quantity');
        console.log('🔍 DEBUG: Found variant inputs:', allVariantInputs.length);
    }
    
    variantRows.forEach((row, index) => {
        console.log(`🔍 DEBUG: Processing variant row ${index + 1}`);
        
        // Use the correct class-based selectors
        const nameInput = row.querySelector('.variant-name');
        const priceInput = row.querySelector('.variant-price');
        const quantityInput = row.querySelector('.variant-quantity');
        const weightInput = row.querySelector('.variant-weight-grams');
        
        console.log('🔍 DEBUG: Inputs found:', { 
            nameInput: !!nameInput, 
            priceInput: !!priceInput, 
            quantityInput: !!quantityInput 
        });
        
        // FIXED: Don't require ALL inputs - use defaults for missing ones
        const name = nameInput ? nameInput.value.trim() : '';
        const price = priceInput ? priceInput.value.trim() : '0';
        const quantity = quantityInput ? quantityInput.value.trim() : '999';
        const weightGrams = weightInput ? parseFloat(weightInput.value) : 0;
        
        console.log('🔍 DEBUG: Input values:', { name, price, quantity });
        
        // Only require name and price to be valid
        if (name && price) {
            variants.push({
                title: name,
                price: price,
                inventory_quantity: parseInt(quantity) || 999,
                weight_grams: weightGrams > 0 ? weightGrams : null
            });
            console.log('✅ DEBUG: Added variant:', { title: name, price, inventory_quantity: parseInt(quantity) || 999 });
        } else {
            console.log('⚠️ DEBUG: Skipping variant with empty name/price:', { name, price, quantity });
        }
    });
    
    console.log('🎯 FINAL: Collected variants data:', variants);
    console.log('🎯 FINAL: Total variants collected:', variants.length);
    
    // CRITICAL: If no variants collected, log error but don't return empty
    if (variants.length === 0) {
        console.error('❌ CRITICAL: No variants collected! This will cause default variants to be used.');
        console.error('❌ Check that variant rows exist in the DOM and have the correct classes.');
    }
    
    return variants;
}

// Function to update variant preview displays
function updateVariantPreview() {
    console.log('🎨 UPDATING VARIANT PREVIEW DISPLAYS');
    const variants = collectVariantsData();
    
    // Update both preview areas
    const previewDisplay = document.getElementById('preview-variants-display');
    const reviewDisplay = document.getElementById('review-variants-display');
    
    if (variants.length === 0) {
        console.log('⚠️ NO VARIANTS FOUND - showing fallback message');
        const fallbackHTML = '<div class="col-12"><div class="alert alert-warning">No variants configured. Please add variants above.</div></div>';
        if (previewDisplay) previewDisplay.innerHTML = fallbackHTML;
        if (reviewDisplay) reviewDisplay.innerHTML = fallbackHTML;
        return;
    }
    
    console.log('✅ BUILDING VARIANT PREVIEW HTML for', variants.length, 'variants');
    
    let variantHTML = '';
    variants.forEach(variant => {
        variantHTML += `<div class="col-md-3 mb-2">
            <div class="bg-primary text-white p-2 rounded text-center small">
                <strong>${variant.title}</strong><br>
                £${variant.price}<br>
                <span class="opacity-75">${variant.inventory_quantity} in stock</span>
            </div>
        </div>`;
    });
    
    console.log('📝 GENERATED VARIANT HTML:', variantHTML);
    
    // Update both displays
    if (previewDisplay) {
        previewDisplay.innerHTML = variantHTML;
        console.log('✅ UPDATED PREVIEW DISPLAY');
    }
    if (reviewDisplay) {
        reviewDisplay.innerHTML = variantHTML;
        console.log('✅ UPDATED REVIEW DISPLAY');
    }
}

// Store selected files for ordering before upload
let selectedFilesList = [];

function initializeSimpleUpload() {
    const fileInput = document.getElementById('simple-file-input');
    if (fileInput) {
        fileInput.addEventListener('change', function(e) {
            const files = Array.from(e.target.files);
            if (files.length === 0) return;

            console.log(`🚀 FILES SELECTED: ${files.length} files`);
            
            // Add new files to the list (avoid duplicates)
            files.forEach(file => {
                // Check if file already exists by name and size
                const exists = selectedFilesList.some(f => f.name === file.name && f.size === file.size);
                if (!exists) {
                    selectedFilesList.push(file);
                }
            });
            
            // Show preview/ordering UI
            showImagePreviewAndOrdering();
            
            // Clear the input so same file can be selected again
            fileInput.value = '';
        });
    }

    // Also make upload area clickable
    const uploadArea = document.getElementById('simple-upload-area');
    if (uploadArea) {
        uploadArea.addEventListener('click', function(e) {
            // Don't trigger if clicking on buttons or the image list
            if (e.target.tagName !== 'BUTTON' && !e.target.closest('.image-preview-list')) {
                startSimpleUpload();
            }
        });
    }
}

function showImagePreviewAndOrdering() {
    const uploadArea = document.getElementById('simple-upload-area');
    const statusDiv = document.getElementById('upload-status');
    
    if (selectedFilesList.length === 0) {
        // Show default upload UI
        uploadArea.innerHTML = `
            <div id="upload-content">
                <i class="fas fa-images fa-3x text-info mb-3"></i>
                <h4>Upload Ready-Framed Images</h4>
                <p class="text-muted">
                    Click here to select images for upload<br>
                    Supports: PNG, JPG, JPEG, GIF, BMP, WebP (up to 200MB each)<br>
                    <button class="btn btn-info mt-2" onclick="startSimpleUpload()">
                        <i class="fas fa-upload me-2"></i>Select Images
                    </button>
                </p>
                <div id="upload-status" class="mt-3"></div>
                <div id="selected-files" class="mt-3"></div>
            </div>
        `;
        statusDiv.innerHTML = '';
        return;
    }
    
    // Show image preview with ordering controls
    let imagesHTML = `
        <div class="w-100">
            <h5 class="mb-3">
                <i class="fas fa-images me-2"></i>
                ${selectedFilesList.length} Image(s) Selected - Reorder Before Processing
            </h5>
            <div class="image-preview-list" id="image-preview-list">
    `;
    
    selectedFilesList.forEach((file, index) => {
        const fileSize = (file.size / 1024 / 1024).toFixed(2);
        const imageUrl = URL.createObjectURL(file);
        
        imagesHTML += `
            <div class="image-preview-item mb-3 p-3 border rounded" data-index="${index}" draggable="true">
                <div class="d-flex align-items-center">
                    <div class="drag-handle me-3" style="cursor: move;">
                        <i class="fas fa-grip-vertical fa-2x text-muted"></i>
                    </div>
                    <div class="image-thumbnail-wrapper me-3" style="position: relative; width: 100px; height: 100px;">
                        <div class="image-thumbnail" style="width: 100%; height: 100%; overflow: hidden; border: 1px solid #ddd; border-radius: 4px;">
                            <img src="${imageUrl}" alt="${file.name}" style="width: 100%; height: 100%; object-fit: cover;">
                        </div>
                        <button type="button" class="image-remove-x" onclick="event.stopPropagation(); removeImage(${index});" title="Remove image" aria-label="Remove image">
                            <i class="fas fa-times"></i>
                        </button>
                    </div>
                    <div class="flex-grow-1">
                        <div class="fw-bold">${file.name}</div>
                        <div class="text-muted small">${fileSize} MB</div>
                        <div class="badge bg-info mt-1">Position ${index + 1}</div>
                    </div>
                    <div class="order-controls">
                        <button class="btn btn-sm btn-outline-secondary me-1" onclick="moveImageUp(${index})" ${index === 0 ? 'disabled' : ''}>
                            <i class="fas fa-arrow-up"></i>
                        </button>
                        <button class="btn btn-sm btn-outline-secondary me-1" onclick="moveImageDown(${index})" ${index === selectedFilesList.length - 1 ? 'disabled' : ''}>
                            <i class="fas fa-arrow-down"></i>
                        </button>
                        <button class="btn btn-sm btn-outline-danger" onclick="removeImage(${index})">
                            <i class="fas fa-times"></i>
                        </button>
                    </div>
                </div>
            </div>
        `;
    });
    
    imagesHTML += `
            </div>
            <div id="upload-status" class="mt-3"></div>
            <div class="mt-3 d-flex gap-2">
                <button class="btn btn-info" onclick="startSimpleUpload()">
                    <i class="fas fa-plus me-2"></i>Add More Images
                </button>
                <button class="btn btn-success" onclick="uploadAllImages()">
                    <i class="fas fa-rocket me-2"></i>Start Processing with ${selectedFilesList.length} Image(s)
                </button>
                <button class="btn btn-outline-secondary" onclick="clearAllImages()">
                    <i class="fas fa-trash me-2"></i>Clear All
                </button>
            </div>
            <div class="mt-2 text-muted small">
                <i class="fas fa-info-circle me-1"></i>
                The first image will be analyzed by AI. All images will be uploaded to Shopify in the order shown above.
            </div>
        </div>
    `;
    
    uploadArea.innerHTML = imagesHTML;
    
    // Initialize drag and drop
    initializeDragAndDrop();
}

function moveImageUp(index) {
    if (index === 0) return;
    [selectedFilesList[index], selectedFilesList[index - 1]] = [selectedFilesList[index - 1], selectedFilesList[index]];
    showImagePreviewAndOrdering();
}

function moveImageDown(index) {
    if (index === selectedFilesList.length - 1) return;
    [selectedFilesList[index], selectedFilesList[index + 1]] = [selectedFilesList[index + 1], selectedFilesList[index]];
    showImagePreviewAndOrdering();
}

function removeImage(index) {
    selectedFilesList.splice(index, 1);
    showImagePreviewAndOrdering();
}

function clearAllImages() {
    if (confirm(`Are you sure you want to remove all ${selectedFilesList.length} selected images?`)) {
        selectedFilesList = [];
        showImagePreviewAndOrdering();
    }
}

function initializeDragAndDrop() {
    const list = document.getElementById('image-preview-list');
    if (!list) return;
    
    let draggedElement = null;
    
    list.querySelectorAll('.image-preview-item').forEach(item => {
        item.addEventListener('dragstart', function(e) {
            draggedElement = this;
            this.style.opacity = '0.5';
            e.dataTransfer.effectAllowed = 'move';
        });
        
        item.addEventListener('dragend', function(e) {
            this.style.opacity = '1';
            draggedElement = null;
        });
        
        item.addEventListener('dragover', function(e) {
            e.preventDefault();
            e.dataTransfer.dropEffect = 'move';
        });
        
        item.addEventListener('drop', function(e) {
            e.preventDefault();
            if (draggedElement && draggedElement !== this) {
                const fromIndex = parseInt(draggedElement.dataset.index);
                const toIndex = parseInt(this.dataset.index);
                
                // Move in array
                const [moved] = selectedFilesList.splice(fromIndex, 1);
                selectedFilesList.splice(toIndex, 0, moved);
                
                // Refresh display
                showImagePreviewAndOrdering();
            }
        });
    });
}

function uploadAllImages() {
    if (selectedFilesList.length === 0) {
        alert('No images selected. Please select images first.');
        return;
    }
    
    console.log(`📤 UPLOADING ${selectedFilesList.length} IMAGES IN ORDER`);
    
    // Show upload progress - ensure statusDiv exists
    const statusDiv = document.getElementById('upload-status');
    if (statusDiv) {
        statusDiv.innerHTML = `<div class="alert alert-info">
            <i class="fas fa-spinner fa-spin me-2"></i>Uploading ${selectedFilesList.length} image(s)...
        </div>`;
    } else {
        console.warn('upload-status div not found, continuing anyway');
    }
    
    // Upload files in the current order
    uploadFiles(selectedFilesList);
}

function uploadFiles(files) {
    const formData = new FormData();
    
    // Add files
    for (let file of files) {
        formData.append('files', file);
    }
    
    // Add form data from the page
    const customPrompt = (typeof getPromptValue === 'function') ? getPromptValue('customPrompt') : (document.getElementById('customPrompt')?.value || '');
    formData.append('custom_prompt', customPrompt);
    const reviewBeforePublish = document.getElementById('publishModeReview')?.checked !== false;
    formData.append('review_before_publish', reviewBeforePublish ? 'true' : 'false');
    
    // Collect variant data from the page
    console.log('🚀 ABOUT TO COLLECT VARIANTS');
    const variantsData = collectVariantsData();
    console.log('🚀 COLLECTED VARIANTS RESULT:', variantsData);
    formData.append('variants_data', JSON.stringify(variantsData));
    
    console.log('📤 UPLOADING FILES:', files.length);
    
    // Create abort controller for timeout
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), 120000); // 2 minute timeout
    
    fetch('/upload_ready', {
        method: 'POST',
        body: formData,
        signal: controller.signal
    })
    .then(response => {
        clearTimeout(timeoutId);
        
        // Check if response is OK before parsing JSON
        if (!response.ok) {
            return response.text().then(text => {
                throw new Error(`Server error: ${response.status} ${response.statusText}. ${text}`);
            });
        }
        
        // Try to parse JSON, but handle non-JSON responses
        const contentType = response.headers.get('content-type');
        if (contentType && contentType.includes('application/json')) {
            return response.json();
        } else {
            return response.text().then(text => {
                throw new Error(`Expected JSON but got: ${contentType}. Response: ${text.substring(0, 200)}`);
            });
        }
    })
    .then(data => {
        console.log('✅ UPLOAD SUCCESS:', data);
        handleUploadSuccess(data, files);
        // Immediately refresh the queue
        setTimeout(refreshQueue, 500);
    })
    .catch(error => {
        clearTimeout(timeoutId);
        console.error('❌ UPLOAD ERROR:', error);
        
        // Provide more specific error messages
        let errorMessage = 'Upload failed';
        if (error.name === 'AbortError') {
            errorMessage = 'Upload timed out after 2 minutes. The server may be slow or unresponsive. Please try again.';
        } else if (error.message) {
            errorMessage = error.message;
        } else {
            errorMessage = String(error);
        }
        
        handleUploadError({ message: errorMessage }, files);
    });
}

function handleUploadSuccess(data, files) {
    var statusDiv = document.getElementById('upload-status');
    if (!data) {
        console.error('Upload success but no data from server');
        if (statusDiv) statusDiv.innerHTML = '<div class="alert alert-warning"><i class="fas fa-exclamation-triangle me-2"></i>Upload may have succeeded but server did not return task info. Refresh the queue and click Start Processing if you see your images.</div>';
        return;
    }
    // Support both task_id and id in response
    var taskIds = (data.tasks || []).map(function(t) { return t.task_id || t.id || ''; }).filter(Boolean);
    
    if (statusDiv) {
        statusDiv.innerHTML = '<div class="alert alert-success">' +
            '<i class="fas fa-check-circle me-2"></i>Successfully uploaded ' + (files ? files.length : 0) + ' image(s)! ' +
            '<br><strong>Starting AI processing now...</strong></div>';
    }
    
    selectedFilesList = [];
    showImagePreviewAndOrdering();
    enableStartProcessingButton();
    var fileInput = document.getElementById('simple-file-input');
    if (fileInput) fileInput.value = '';
    
    if (typeof refreshQueue === 'function') refreshQueue();
    
    if (taskIds.length > 0) {
        console.log('🚀 Auto-starting processing for task IDs from upload:', taskIds);
        startProcessingForTaskIds(taskIds, { showButtonFeedback: false });
    } else {
        console.warn('No task_id in upload response; refresh the queue and click Start Processing');
        if (statusDiv) statusDiv.innerHTML = '<div class="alert alert-info"><i class="fas fa-info-circle me-2"></i>Upload complete. Click "Start AI Processing" below to process your images.</div>';
    }
}

function handleUploadError(error, files) {
    const statusDiv = document.getElementById('upload-status');
    
    statusDiv.innerHTML = `<div class="alert alert-danger">
        <i class="fas fa-exclamation-triangle me-2"></i>Upload failed: ${error.message || error}
    </div>`;
    
    // Update individual file statuses
    for (let i = 0; i < files.length; i++) {
        const statusBadge = document.getElementById(`status-${i}`);
        if (statusBadge) {
            statusBadge.className = 'badge bg-danger';
            statusBadge.textContent = 'Failed';
        }
    }
}

// Initialize ready-framed workflow: create dropzone with remove controls when element exists
function initializeReadyDropzone() {
    const el = document.getElementById('ready-images-dropzone');
    if (typeof createReadyImagesDropzone === 'function' && el) {
        createReadyImagesDropzone();
    } else {
        console.log('Initializing simple upload system...');
        initializeSimpleUpload();
    }
}

// Build the JSON payload sent to /start_processing (shared by button and auto-start)
function buildStartProcessingPayload() {
    const currentVariants = collectVariantsData();
    return {
        custom_prompt: (typeof getPromptValue === 'function') ? getPromptValue('customPrompt') : ((document.getElementById('customPrompt') && document.getElementById('customPrompt').value) || ''),
        business_name: (document.getElementById('businessName') && document.getElementById('businessName').value) || '',
        vendor: (document.getElementById('productVendor') && document.getElementById('productVendor').value) || '',
        review_before_publish: (document.getElementById('publishModeReview') && document.getElementById('publishModeReview').checked) !== false,
        selected_sales_channels: typeof getSelectedSalesChannels === 'function' ? getSelectedSalesChannels() : [],
        selected_markets: typeof getSelectedMarkets === 'function' ? getSelectedMarkets() : [],
        variants_data: currentVariants,
        tags_manual: (document.querySelector('input[name="tags_manual"]') && document.querySelector('input[name="tags_manual"]').checked) || false,
        manual_tags: (document.querySelector('input[name="manual_tags"]') && document.querySelector('input[name="manual_tags"]').value) || '',
        collections_manual: (document.querySelector('input[name="collections_manual"]') && document.querySelector('input[name="collections_manual"]').checked) || false,
        manual_collections: (document.querySelector('input[name="manual_collections"]') && document.querySelector('input[name="manual_collections"]').value) || '',
    };
}

// Start processing for given task IDs (used by both button and auto-start after upload)
function startProcessingForTaskIds(taskIds, options) {
    options = options || {};
    var showButtonFeedback = options.showButtonFeedback !== false;
    var startBtn = document.getElementById('startProcessBtn');
    
    if (taskIds.length === 0) return;
    
    console.log('🎯 Starting processing for ' + taskIds.length + ' task(s):', taskIds);
    
    if (showButtonFeedback && startBtn) {
        startBtn.disabled = true;
        startBtn.innerHTML = '<i class="fas fa-spinner fa-spin me-3"></i><span class="fw-bold">Processing ' + taskIds.length + ' Images...</span>';
    }
    
    var payload = buildStartProcessingPayload();
    var completed = 0;
    
    // Sequential loop: each /start_processing call completes before the next fires
    (async function() {
        for (var i = 0; i < taskIds.length; i++) {
            var taskId = taskIds[i];
            try {
                var response = await fetch('/start_processing/' + taskId, {
                    method: 'POST',
                    headers: { 'Content-Type': 'application/json' },
                    body: JSON.stringify(payload)
                });
                var data = await response.json();
                if (data && data.error && !response.ok) {
                    console.error('❌ Start processing failed for ' + taskId + ':', data.error);
                } else {
                    console.log('✅ Processing started for task ' + taskId, data);
                }
            } catch(err) {
                console.error('❌ Failed to start processing for task ' + taskId + ':', err);
            }
            completed++;
        }
        // All requests finished
        if (showButtonFeedback && startBtn) {
            startBtn.innerHTML = '<i class="fas fa-check me-3"></i><span class="fw-bold">AI Processing Started!</span>';
            setTimeout(function() {
                startBtn.disabled = false;
                startBtn.innerHTML = '<i class="fas fa-rocket me-3"></i><span class="fw-bold">Start AI Processing & Shopify Upload</span><i class="fas fa-arrow-right ms-3"></i>';
            }, 3000);
        }
        var alertDiv = document.createElement('div');
        alertDiv.className = 'alert alert-success position-fixed';
        alertDiv.style.cssText = 'top: 20px; right: 20px; z-index: 9999; width: 350px;';
        alertDiv.innerHTML = '<i class="fas fa-rocket me-2"></i> AI processing started for ' + taskIds.length + ' image(s)! Check the queue below for progress.';
        document.body.appendChild(alertDiv);
        setTimeout(function() { alertDiv.remove(); }, 5000);
        if (typeof refreshQueue === 'function') setTimeout(refreshQueue, 500);
    })();
}

// Function to start AI processing for all uploaded tasks (button click: discover task IDs from queue DOM)
function startAIProcessing() {
    console.log('🚀 START AI PROCESSING CALLED!');
    
    var queueContainer = document.getElementById('queue-container');
    if (!queueContainer) {
        console.error('❌ Queue container not found!');
        alert('Queue not found. Please refresh the page.');
        return;
    }
    
    var taskCards = queueContainer.querySelectorAll('.card[id^="task-"]');
    if (taskCards.length === 0) {
        alert('No uploaded images found. Please upload images first.');
        return;
    }
    
    var taskIds = [];
    taskCards.forEach(function(card) {
        var taskId = card.id.replace('task-', '');
        var statusBadge = card.querySelector('.badge');
        var badgeText = statusBadge ? statusBadge.textContent.trim().toLowerCase() : '';
        if (badgeText === 'uploaded' || badgeText === 'queued') {
            taskIds.push(taskId);
        }
    });
    
    if (taskIds.length === 0) {
        alert('No uploaded tasks ready for processing. Upload images first or refresh the queue.');
        return;
    }
    
    startProcessingForTaskIds(taskIds, { showButtonFeedback: true });
}

// Function to enable the Start Processing button
function enableStartProcessingButton() {
    const startBtn = document.getElementById('startProcessBtn');
    if (startBtn) {
        startBtn.disabled = false;
        console.log('✅ Start Processing button ENABLED');
    }
}

// Function to check if we have uploaded tasks and enable button accordingly
function checkAndEnableButton() {
    // Wait a bit for queue to load, then check for uploaded tasks
    setTimeout(() => {
        const queueContainer = document.getElementById('queue-container');
        if (queueContainer) {
            const taskCards = queueContainer.querySelectorAll('.card[id^="task-"]');
            
            // Check if any task has "uploaded" status
            let hasUploadedTasks = false;
            taskCards.forEach(card => {
                const statusBadge = card.querySelector('.badge');
                var badgeText = statusBadge ? statusBadge.textContent.trim().toLowerCase() : '';
                if (badgeText === 'uploaded' || badgeText === 'queued') {
                    hasUploadedTasks = true;
                }
            });
            
            if (hasUploadedTasks || taskCards.length > 0) {
                enableStartProcessingButton();
                console.log(`📋 Found ${taskCards.length} tasks, enabled button`);
            }
        }
    }, 1000);
}

// Helper functions to get publishing settings
function getSelectedSalesChannels() {
    const selectedChannels = [];
    // Get selected publishing channels from checkboxes
    const checkboxes = document.querySelectorAll('.sales-channel-checkbox:checked');
    checkboxes.forEach(checkbox => {
        selectedChannels.push(checkbox.value);
    });
    console.log('📻 Selected publishing channels:', selectedChannels);
    return selectedChannels;
}

function getSelectedMarkets() {
    const selectedMarkets = [];
    // Get selected catalogs from checkboxes
    const checkboxes = document.querySelectorAll('.market-checkbox:checked');
    checkboxes.forEach(checkbox => {
        selectedMarkets.push(checkbox.value);
    });
    console.log('📦 Selected catalogs:', selectedMarkets);
    return selectedMarkets;
}

// Make functions globally accessible
window.startAIProcessing = startAIProcessing;
window.enableStartProcessingButton = enableStartProcessingButton;
window.getSelectedSalesChannels = getSelectedSalesChannels;
window.getSelectedMarkets = getSelectedMarkets;

// Initialize when DOM is loaded
// Start button (#startProcessBtn) is controlled by index.html inline script (workflow filter, pending upload, clear dropzone, runProcessing)
document.addEventListener('DOMContentLoaded', function() {
    initializeReadyDropzone();

    // Also initialize other workflow functions if they exist
    if (typeof initializeImageDropzone === 'function') {
        initializeImageDropzone();
    }
});
