(function (window, document) {
    'use strict';

    var SmithWidget = {
        iframe: null,
        config: {},
        isOpen: false,

        init: function (options) {
            this.config = options || {};
            if (!this.config.agentId) {
                console.error('SmithWidget: agentId is required');
                return;
            }
            this.createIframe();
            this.setupListeners();
        },

        createIframe: function () {
            var iframe = document.createElement('iframe');

            // Robust way to find the script tag to determine baseUrl
            var currentScript = document.getElementById('mw') ||
                document.currentScript ||
                (function () {
                    var scripts = document.getElementsByTagName('script');
                    for (var i = 0; i < scripts.length; i++) {
                        if (scripts[i].src && scripts[i].src.indexOf('/widget.js') > -1) {
                            return scripts[i];
                        }
                    }
                    return scripts[scripts.length - 1];
                })();

            var baseUrl = currentScript && currentScript.src ? currentScript.src.split('/widget.js')[0] : window.location.origin;

            iframe.src = baseUrl + '/embed/' + this.config.agentId;
            iframe.id = 'smith-widget-iframe';
            iframe.style.cssText = [
                'position: fixed',
                'bottom: 20px',
                'right: 20px',
                'width: 70px',
                'height: 70px',
                'border: none',
                'z-index: 2147483647',
                'transition: all 0.3s cubic-bezier(0.4, 0, 0.2, 1)',
                'border-radius: 35px',
                'box-shadow: 0 4px 20px rgba(0,0,0,0.15)',
                'background: transparent'
            ].join(';');

            iframe.allow = 'microphone';
            iframe.setAttribute('allowtransparency', 'true');

            document.body.appendChild(iframe);
            this.iframe = iframe;
        },

        setupListeners: function () {
            var self = this;

            window.addEventListener('message', function (event) {
                var data = event.data;
                if (!data || typeof data !== 'object') return;

                if (data.type === 'smith:ready') {
                    console.log('Smith Widget loaded');
                }

                if (data.type === 'smith:resize') {
                    self.isOpen = data.isOpen;
                    var isMobile = window.innerWidth < 480;

                    if (data.isOpen) {
                        if (isMobile) {
                            self.iframe.style.width = '100%';
                            self.iframe.style.height = '100%';
                            self.iframe.style.bottom = '0';
                            self.iframe.style.right = '0';
                            self.iframe.style.borderRadius = '0';
                        } else {
                            self.iframe.style.width = data.width || '380px';
                            self.iframe.style.height = data.height || '600px';
                            self.iframe.style.borderRadius = '16px';
                        }
                        self.iframe.style.boxShadow = '0 10px 40px rgba(0,0,0,0.2)';
                    } else {
                        self.iframe.style.width = '70px';
                        self.iframe.style.height = '70px';
                        self.iframe.style.borderRadius = '35px';
                        self.iframe.style.bottom = '20px';
                        self.iframe.style.right = '20px';
                        self.iframe.style.boxShadow = '0 4px 20px rgba(0,0,0,0.15)';
                    }
                }

                if (data.type === 'smith:position') {
                    if (data.position === 'bottom-left') {
                        self.iframe.style.right = 'auto';
                        self.iframe.style.left = '20px';
                    } else {
                        self.iframe.style.left = 'auto';
                        self.iframe.style.right = '20px';
                    }
                }
            });

            // Handle window resize
            window.addEventListener('resize', function () {
                if (self.isOpen && window.innerWidth < 480) {
                    self.iframe.style.width = '100%';
                    self.iframe.style.height = '100%';
                    self.iframe.style.bottom = '0';
                    self.iframe.style.right = '0';
                    self.iframe.style.left = '0';
                    self.iframe.style.borderRadius = '0';
                }
            });
        }
    };

    // Expose globally
    window.mw = function (method, options) {
        if (SmithWidget[method]) {
            SmithWidget[method](options);
        }
    };

    // Process queued calls
    if (window.mw && window.mw.q) {
        var queue = window.mw.q;
        for (var i = 0; i < queue.length; i++) {
            window.mw.apply(null, queue[i]);
        }
    }

})(window, document);
