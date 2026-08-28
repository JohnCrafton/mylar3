/* Optional navigation overlay.  Only loaded when ENABLE_NAV_MENU is on, so
 * nothing here runs for users who have not opted in.
 *
 * Written for the jQuery 1.7.2 that base.html ships: no arrow functions, no
 * let/const, no Promise, no $.fn.on() shorthand that postdates 1.7.
 */
(function ($) {
    'use strict';

    var menuLoaded = false;
    var lastFocused = null;

    function esc(text) {
        return $('<div/>').text(text == null ? '' : text).html();
    }

    function report(message) {
        // showMsg() lives in script.js, which base.html loads before this file.
        if (typeof showMsg === 'function') {
            showMsg(esc(message), false, true, 3000);
        }
    }

    function runTask(endpoint, label) {
        report('Running ' + label + '&hellip;');
        $.ajax({
            url: endpoint,
            type: 'GET',
            success: function () {
                report(label + ' started.');
            },
            error: function () {
                report(label + ' failed &mdash; check the log.');
            }
        });
    }

    function urlFor(entry) {
        var url = entry.endpoint;
        if (entry.params) {
            var query = $.param(entry.params);
            if (query) {
                url += '?' + query;
            }
        }
        return url;
    }

    function entryHtml(entry) {
        var label = esc(entry.label);
        var confirmAttr = entry.confirm ? ' data-confirm="1"' : '';
        if (entry.kind === 'page') {
            return '<li><a href="' + esc(urlFor(entry)) + '"' + confirmAttr +
                ' data-label="' + label + '">' + label + '</a></li>';
        }
        return '<li><a href="#" class="mylar_nav_task" data-endpoint="' +
            esc(urlFor(entry)) + '"' + confirmAttr +
            ' data-label="' + label + '">' + label + '</a></li>';
    }

    function renderGroup(target, entries) {
        var html = '';
        for (var i = 0; i < entries.length; i++) {
            html += entryHtml(entries[i]);
        }
        $(target).html(html);
    }

    function loadMenu(callback) {
        if (menuLoaded) {
            callback();
            return;
        }
        $.ajax({
            url: 'navMenu',
            dataType: 'json',
            success: function (data) {
                renderGroup('#mylar_nav_articles', data.articles || []);
                renderGroup('#mylar_nav_actions', data.actions || []);
                menuLoaded = true;
                callback();
            },
            error: function () {
                $('#mylar_nav_articles').html('<li class="mylar_nav_error">Menu unavailable.</li>');
                $('#mylar_nav_actions').empty();
                callback();
            }
        });
    }

    function openMenu() {
        lastFocused = document.activeElement;
        loadMenu(function () {
            $('#mylar_nav_overlay').attr('aria-hidden', 'false').fadeIn(120, function () {
                $('#mylar_nav_panel').find('a, input').filter(':visible').first().focus();
            });
            $('#mylar_nav_button').attr('aria-expanded', 'true');
        });
    }

    function closeMenu() {
        $('#mylar_nav_overlay').attr('aria-hidden', 'true').fadeOut(120);
        $('#mylar_nav_button').attr('aria-expanded', 'false');
        if (lastFocused && lastFocused.focus) {
            lastFocused.focus();
        }
    }

    function isOpen() {
        return $('#mylar_nav_overlay').is(':visible');
    }

    $(document).ready(function () {
        if ($('#mylar_nav_overlay').length === 0) {
            return;
        }

        $('#mylar_nav_button').click(function (event) {
            event.preventDefault();
            if (isOpen()) {
                closeMenu();
            } else {
                openMenu();
            }
        });

        $('#mylar_nav_close').click(function (event) {
            event.preventDefault();
            closeMenu();
        });

        // clicking the backdrop closes; clicking inside the panel does not
        $('#mylar_nav_overlay').click(function (event) {
            if (event.target === this) {
                closeMenu();
            }
        });

        $(document).keydown(function (event) {
            if (event.which === 27 && isOpen()) {
                closeMenu();
            }
        });

        // delegated so it survives the menu being rendered after page load
        $('#mylar_nav_groups').delegate('a', 'click', function (event) {
            var link = $(this);
            var label = link.attr('data-label') || link.text();

            if (link.attr('data-confirm') === '1') {
                if (!window.confirm(label + ' — are you sure?')) {
                    event.preventDefault();
                    return;
                }
            }

            if (link.hasClass('mylar_nav_task')) {
                event.preventDefault();
                closeMenu();
                runTask(link.attr('data-endpoint'), label);
            }
            // 'page' entries fall through to the browser's normal navigation
        });
    });
}(jQuery));
