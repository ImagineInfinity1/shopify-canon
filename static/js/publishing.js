// Publishing & Catalogs Management
let loadedPublishingSettings = null;
let publishingSettingsLoaded = false;

// Load publishing settings when page loads
function loadPublishingSettings() {
    console.log('📡 loadPublishingSettings() called');
    return fetch('/get_publishing_settings')
        .then(response => {
            if (!response.ok) {
                throw new Error(`HTTP ${response.status}: ${response.statusText}`);
            }
            return response.json();
        })
        .then(data => {
            if (data.no_shop || data.error) {
                console.warn('Publishing settings: no store or error', data.error || 'No store connected');
                showPublishingError(data.error || 'No Shopify store connected. Connect your store in Settings.');
                return data; // Return so callers can detect no_shop
            }
            
            loadedPublishingSettings = data;
            publishingSettingsLoaded = true;
            populatePublishingSettings(data);
            console.log('✅ Publishing & catalog settings loaded from Shopify');
            return data;
        })
        .catch(error => {
            console.error('❌ Error loading publishing settings:', error);
            showPublishingError('Failed to load publishing settings. Click to retry.');
        });
}

// Show error state in publishing/catalog containers with retry button
function showPublishingError(message) {
    const channelsContainer = document.getElementById('salesChannelsContainer');
    const catalogsContainer = document.getElementById('marketsContainer');
    
    const errorHtml = `
        <div class="text-danger small">
            <i class="fas fa-exclamation-triangle me-1"></i>${message}
            <br><a href="#" onclick="event.preventDefault(); loadPublishingSettings();" class="text-primary small mt-1 d-inline-block">
                <i class="fas fa-sync me-1"></i>Retry
            </a>
        </div>
    `;
    
    if (channelsContainer) channelsContainer.innerHTML = errorHtml;
    if (catalogsContainer) catalogsContainer.innerHTML = errorHtml;
}

// Populate the publishing settings in the UI
function populatePublishingSettings(settings) {
    if (!settings.publications || !settings.markets) {
        console.warn('No publishing data available');
        return;
    }
    
    // Populate publishing channels
    const channelsContainer = document.getElementById('salesChannelsContainer');
    if (channelsContainer && settings.publications.edges) {
        console.log('📺 Populating publishing channels...');
        channelsContainer.innerHTML = '';
        
        let channelIndex = 0;
        settings.publications.edges.forEach((edge) => {
            const publication = edge.node;
            
            console.log(`Adding publishing channel: ${publication.name}`);
            const channelHtml = `
                <div class="form-check mb-2">
                    <input class="form-check-input sales-channel-checkbox" 
                           type="checkbox" 
                           value="${publication.id}" 
                           id="channel-${channelIndex}"
                           checked>
                    <label class="form-check-label d-flex justify-content-between" 
                           for="channel-${channelIndex}">
                        <span>
                            <i class="fas fa-store me-2"></i>
                            ${publication.name}
                        </span>
                        <small class="text-muted">${publication.app?.title || publication.name}</small>
                    </label>
                </div>
            `;
            channelsContainer.innerHTML += channelHtml;
            channelIndex++;
        });
        if (channelIndex === 0) {
            channelsContainer.innerHTML = '<div class="text-muted small">No publishing channels found</div>';
        }
        console.log(`✅ Added ${channelIndex} publishing channels`);
    } else {
        console.error('❌ Publishing channels container not found or no data');
    }
    
    // Populate catalogs (markets)
    const catalogsContainer = document.getElementById('marketsContainer');
    if (catalogsContainer && settings.markets.edges) {
        console.log('📦 Populating catalogs...');
        catalogsContainer.innerHTML = '';
        
        settings.markets.edges.forEach((edge, index) => {
            const market = edge.node;
            console.log(`Adding catalog: ${market.name}`);
            const regionCount = market.regions?.edges?.length || 0;
            const regionText = regionCount > 0 ? ` (${regionCount} regions)` : '';
            const primaryBadge = market.primary ? '<span class="badge bg-primary ms-2">Primary</span>' : '';
            
            const catalogHtml = `
                <div class="form-check mb-2">
                    <input class="form-check-input market-checkbox" 
                           type="checkbox" 
                           value="${market.id}" 
                           id="market-${index}"
                           checked>
                    <label class="form-check-label d-flex justify-content-between align-items-center" 
                           for="market-${index}">
                        <span>
                            <i class="fas fa-globe me-2"></i>
                            ${market.name}${regionText}
                        </span>
                        ${primaryBadge}
                    </label>
                </div>
            `;
            catalogsContainer.innerHTML += catalogHtml;
        });
        if (settings.markets.edges.length === 0) {
            catalogsContainer.innerHTML = '<div class="text-muted small">No catalogs found</div>';
        }
        console.log(`✅ Added ${settings.markets.edges.length} catalogs`);
    } else {
        console.error('❌ Catalogs container not found or no data');
    }
    
    // CRITICAL FIX: Automatically save default selections after checkboxes are created
    setTimeout(() => {
        savePublishingSelections().then(() => {
            console.log('✅ Default publishing selections saved to session');
        });
        
        // Add change listeners to all publishing/catalog checkboxes so session is updated immediately
        document.querySelectorAll('.sales-channel-checkbox, .market-checkbox').forEach(checkbox => {
            checkbox.addEventListener('change', () => {
                console.log('📡 Publishing/catalog checkbox changed, saving to session...');
                savePublishingSelections();
            });
        });
    }, 100);
}

// Save publishing selections before processing
function savePublishingSelections() {
    const selectedChannels = [];
    const selectedMarkets = [];
    
    // Get selected publishing channels
    document.querySelectorAll('.sales-channel-checkbox:checked').forEach(checkbox => {
        selectedChannels.push(checkbox.value);
    });
    
    // Get selected catalogs
    document.querySelectorAll('.market-checkbox:checked').forEach(checkbox => {
        selectedMarkets.push(checkbox.value);
    });
    
    console.log('Saving publishing selections:', {
        publishing: selectedChannels.length,
        catalogs: selectedMarkets.length
    });
    
    // Send to server to store in session
    return fetch('/save_publishing_selections', {
        method: 'POST',
        headers: {
            'Content-Type': 'application/json'
        },
        body: JSON.stringify({
            selected_channels: selectedChannels,
            selected_markets: selectedMarkets
        })
    })
    .then(response => response.json())
    .then(data => {
        if (data.success) {
            console.log('Publishing selections saved successfully');
            return true;
        } else {
            console.error('Failed to save publishing selections:', data.error);
            return false;
        }
    })
    .catch(error => {
        console.error('Error saving publishing selections:', error);
        return false;
    });
}

// BACKUP: Ensure publishing settings load even if autoSyncShopifyData() fails or never runs
// This catches cases where the main DOMContentLoaded handler errors out before calling autoSyncShopifyData()
(function ensurePublishingSettingsLoad() {
    // If DOM is already loaded, check after a short delay
    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', function() {
            setTimeout(function() {
                if (!publishingSettingsLoaded) {
                    console.warn('⚠️ Publishing settings not loaded by autoSync - loading independently');
                    loadPublishingSettings();
                }
            }, 3000); // Wait 3 seconds to give autoSyncShopifyData() time to work first
        });
    } else {
        // DOM already loaded - check after delay
        setTimeout(function() {
            if (!publishingSettingsLoaded) {
                console.warn('⚠️ Publishing settings not loaded by autoSync - loading independently');
                loadPublishingSettings();
            }
        }, 3000);
    }
})();