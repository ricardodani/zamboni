$(document).ready(function() {
    $("#contribute-why").popup("#contribute-more-info", {
        pointTo: "#contribute-more-info"
    });
    if ($('body').attr('data-paypal-url')) {
        $('div.contribute a.suggested-amount').live('click', function(event) {
            var el = this;
            $.getJSON($(this).attr('href') + '&result_type=json',
                function(json) {
                    if (json.paykey) {
                        $.getScript($('body').attr('data-paypal-url'), function() {
                            dgFlow = new PAYPAL.apps.DGFlow({clicked: el.id});
                            dgFlow.startFlow(json.url);
                        });
                    } else {
                        if (!$('#paypal-error').length) {
                            $(el).closest('div').append('<div id="paypal-error" class="popup"></div>');
                        }
                        $('#paypal-error').text(json.error).popup(el, {pointTo:el}).render();
                    }
                }
            );
            return false;
        });
    }
    if ($('body').attr('data-paypal-url')) {
        if ($('#paypal-result').length) {
            top_dgFlow = top.dgFlow || (top.opener && top.opener.top.dgFlow);
            if (top_dgFlow !== null) {
                top_dgFlow.closeFlow();
                if (top !== null) {
                    top.close();
                }
            }
        }
    }
});

/**
 * Contributions Lightbox
 * TODO(jbalogh): save from amo2009.
 */
var contributions = {
    commentlimit: 255, // paypal-imposed comment length limit

    init: function() {
        // prepare overlay content
        var cb = $('#contribute-box');
        var contrib_limit = parseFloat($('#contrib-too-much').attr('data-max-amount'));
        cb.find('li label').click(function(e) {
            e.preventDefault();
            $(this).siblings(':radio').attr('checked', 'checked');
            $(this).children('input:text').focus();
        }).end()
        .find('input:text').keypress(function() {
            $(this).parent().siblings(':radio').attr('checked', 'checked');
        }).end()
        .find('textarea').keyup(function() {
            var txt = $(this).val(),
                limit = contributions.commentlimit,
                counter = $(this).siblings('.commentlen');
            if (txt.length > limit) {
                $(this).val(txt.substr(0, limit));
            }
            counter.text(limit - Math.min(txt.length, limit));
        }).keyup().end()
        .find('#contrib-too-much').hide().end()
        .find('#contrib-too-little').hide().end()
        .find('#contrib-not-entered').hide().end()
        .find('form').submit(function() {
            var contrib_type = $(this).find('input:checked').val();
            if (contrib_type == 'onetime') {
                var amt = $(this).find('input[name="'+contrib_type+'-amount"]').val();
                $(this).find('.error').hide();
                // parseFloat will catch everything except 1@, +amt will though
                if (isNaN(parseFloat(amt)) || ((+amt) != amt)) {
                    $(this).find('#contrib-not-entered').show();
                    return false;
                }
                if (amt > contrib_limit) {
                    $(this).find('#contrib-too-much').show();
                    return false;
                }
                if (parseFloat(amt) < 0.01) {
                    $(this).find('#contrib-too-little').show();
                    return false;
                }
            }
            if ($('body').attr('data-paypal-url')) {
                var $self = $(this);
                $.ajax({type: 'GET',
                    url: $(this).attr('action') + '?result_type=json',
                    data: $(this).serialize(),
                    success: function(json) {
                        if (json.paykey) {
                            $.getScript($('body').attr('data-paypal-url'), function() {
                                dgFlow = new PAYPAL.apps.DGFlow({clicked: 'contribute-box'});
                                dgFlow.startFlow(json.url);
                                $self.find('span.cancel a').click();
                            });
                        } else {
                            $self.find('#paypal-error').show();
                        }
                    }
                });
                return false;
            } else {
                return true;
            }
        });

        // enable overlay; make sure we have the jqm package available.
        if (!cb.jqm) {
            return;
        }
        cb.jqm({
                overlay: 100,
                overlayClass: 'contrib-overlay',
                onShow: function(hash) {
                    // avoid bleeding-through form elements
                    if ($.browser.opera) this.inputs = $(':input:visible').css('visibility', 'hidden');
                    // clean up, then show box
                    hash.w.find('.error').hide()
                    hash.w
                        .find('input:text').val('').end()
                        .find('textarea').val('').keyup().end()
                        .find('input:radio:first').attr('checked', 'checked').end()
                        .fadeIn();

                },
                onHide: function(hash) {
                    if ($.browser.opera) this.inputs.css('visibility', 'visible');
                    hash.w.find('.error').hide();
                    hash.w.fadeOut();
                    hash.o.remove();
                },
                trigger: '#contribute-button',
                toTop: true
            })
            .jqmAddClose(cb.find('.cancel a'));

        if (window.location.hash === '#contribute-confirm') {
            $('#contribute-button').click();
        }
    }

}
