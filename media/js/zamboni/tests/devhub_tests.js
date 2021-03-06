$(document).ready(function(){

var catFixture = {
    setup: function() {
        this.sandbox = tests.createSandbox('#addon-cats');
        initCatFields();
    },
    teardown: function() {
        this.sandbox.remove();
    }
};

module('initCatFields', catFixture);

test('Default with initial categories', function() {
    var scope = $("#addon-cats-fx", self.sandbox);
    var checkedChoices = $("input:checked", scope);
    equals(checkedChoices.length, 2);
    equals(checkedChoices[0].id, "id_form-0-categories_1");
    equals(checkedChoices[1].id, "id_form-0-categories_2");

    // 2 categories are selected, the other category should be disabled.
    var max = scope.parent("div").attr("data-max-categories");
    equals(parseInt(max), 2);
    var disabledChoices = $("input:disabled", scope);
    equals(disabledChoices.length, 1);
    equals(disabledChoices[0].id, "id_form-0-categories_0");
});

test('Default without initial categories', function() {
    equals($("#addon-cats-tb input:checked", self.sandbox).length, 0);
});


module('addonUploaded', {
    setup: function() {
        this._FormData = z.FormData;
        z.FormData = tests.StubOb(z.FormData, {
            send: function() {}
        });
        this.sandbox = tests.createSandbox('#file-upload-template');
        $.fx.off = true;

        $('#upload-file-input', this.sandbox).addonUploader();

        this.el = $('#upload-file-input', this.sandbox)[0];
        this.el.files = [{
            size: 200,
            name: 'some-addon.xpi'
        }];

        $(this.el).trigger('change');
        // sets all animation durations to 0
        $.fx.off = true;
    },
    teardown: function() {
        $.fx.off = false;
        this.sandbox.remove();
        z.FormData = this._FormData;
        $.fx.off = false;
    }
});

test('JSON error', function() {
    $(this.el).trigger("upload_success_results",
                       [{name: 'somefile.txt'},
                        {'error': "Traceback (most recent call last): ...NameError"}]);

    ok($('#upload-status-bar', this.sandbox).hasClass('bar-fail'));
    equals($('#upload_errors', this.sandbox).text(),
           'Unexpected server error while validating.')
});

test('Too many messages', function() {
    var results = {
        validation: {
            "errors": 7,
            "success": false,
            "warnings": 0,
            "ending_tier": 3,
            "messages": [{
                "message": "Invalid maximum version number",
                "type": "error"
            },
            {
                "message": "Missing translation file",
                "type": "error"
            },
            {
                "message": "Missing translation file",
                "type": "error"
            },
            {
                "message": "Missing translation file",
                "type": "error"
            },
            {
                "message": "Missing translation file",
                "type": "error"
            },
            {
                "message": "Missing translation file",
                "type": "error"
            },
            {
                "message": "Missing translation file",
                "type": "error"
            }],
            "rejected": false,
            "detected_type": "extension",
            "notices": 0,
        },
        error: null,
        full_report_url: '/full-report'
    };

    $(this.el).trigger("upload_success_results",
                       [{name: 'somefile.txt'}, results]);

    equals($('#upload-status-results ul li', this.sandbox).length, 6);
    equals($('#upload-status-results ul li:eq(5)', this.sandbox).text(),
           '…and 2 more');
});


test('form errors are cleared', function() {
    var fxt = this;
    // Simulate django form errors from the POST
    this.sandbox.find('form').prepend(
        '<ul class="errorlist"><li>Duplicate UUID found.</li></ul>');

    $(this.el).trigger("upload_start", [{name: 'somefile.txt'}]);

    equals($('ul.errorlist', this.sandbox).length, 0);
});

test('Notices count as warnings', function() {

    var results = {
        validation: {
            "warnings": 4,
            "notices": 4,
            "errors": 0,
            "success": true,
            "ending_tier": 3,
            "rejected": false,
            "detected_type": "extension"
        },
        error: null,
        full_report_url: '/full-report',
        platforms_to_exclude: []
    };

    $(this.el).trigger("upload_success_results",
                       [{name: 'somefile.txt'}, results]);

    equals($('##upload-status-results strong', this.sandbox).text(),
           'Your add-on passed validation with no errors and 8 warnings.');
});

test('HTML in errors', function() {
    var results = {
        validation: {
            "errors": 1,
            "success": false,
            "warnings": 0,
            "ending_tier": 3,
            "messages": [{
                // TODO(Kumar) when validator is no longer escaped, change this
                "message": "invalid properties in the install.rdf like &lt;em:id&gt;",
                "type": "error"
            }],
            "rejected": false,
            "detected_type": "extension",
            "notices": 0,
        },
        error: null,
        full_report_url: '/full-report'
    };
    $(this.el).trigger("upload_success_results",
                       [{name: 'somefile.txt'}, results]);
    ok($('#upload-status-bar', this.sandbox).hasClass('bar-fail'));
    equals($('#upload_errors', this.sandbox).text(),
           'invalid properties in the install.rdf like <em:id>')
});

test('HTML in filename (on start)', function() {
    $(this.el).trigger("upload_start", [{name: "tester's add-on2.xpi"}]);
    equals($('#upload-status-text', this.sandbox).text(),
           "Uploading tester's add-on2.xpi");
});

test('HTML in filename (on error)', function() {
    var errors = [],
        results = {};
    $(this.el).trigger("upload_errors",
                       [{name: "tester's add-on2.xpi"}, errors, results]);
    equals($('#upload-status-text', this.sandbox).text(),
           "Error with tester's add-on2.xpi");
});

test('HTML in filename (on success)', function() {
    var results = {};
    $(this.el).trigger("upload_success",
                       [{name: "tester's add-on2.xpi"}, results]);
    equals($('#upload-status-text', this.sandbox).text(),
           "Validating tester's add-on2.xpi");
});


module('fileUpload', {
    setup: function() {
        var fxt = this;
        this.sandbox = tests.createSandbox('#file-upload-template');
        initUploadControls();
        this.uploadFile = window.uploadFile;
        this.uploadFileCalled = false;
        // stub out the XHR calls:
        window.uploadFile = function() {
            fxt.uploadFileCalled = true;
            return null;
        };
    },
    teardown: function() {
        this.sandbox.remove();
        window.uploadFile = this.uploadFile;
    }
});

module('preview_edit', {
    setup: function() {
        this.sandbox = tests.createSandbox('#preview-list');
        initUploadPreview();
    },
    teardown: function() {
        this.sandbox.remove();
    }
});

test('Clicking delete screenshot marks checkbox.', function() {
    // $.fx.off sets all animation durations to 0
    $.fx.off = true;
    $(".edit-previews-text a.remove", this.sandbox).trigger('click');
    equals($(".delete input", this.sandbox).attr("checked"), true);
    equals($(".preview:visible", this.sandbox).length, 0);
    $.fx.off = false;
});


module('addon platform chooser', {
    setup: function() {
        this.sandbox = tests.createSandbox('#addon-platform-chooser');
    },
    teardown: function() {
        this.sandbox.remove();
    },
    check: function(sel) {
        $(sel, this.sandbox).attr('checked',true);
        $(sel, this.sandbox).trigger('change');
    }
});

test('platforms > ALL', function() {
    // Check some platforms:
    this.check('input[value="2"]');
    this.check('input[value="3"]');
    // Check ALL platforms:
    this.check('input[value="1"]');
    equals($('input[value="2"]', this.sandbox).attr('checked'), false);
    equals($('input[value="3"]', this.sandbox).attr('checked'), false);
});

test('ALL > platforms', function() {
    // Check ALL platforms:
    this.check('input[value="1"]');
    // Check any other platform:
    this.check('input[value="2"]');
    equals($('input[value="1"]', this.sandbox).attr('checked'), false);
});

test('mobile / desktop', function() {
    // Check ALL desktop platforms:
    this.check('input[value="1"]');
    // Check ALL mobile platforms:
    this.check('input[value="9"]');
    // desktop platforms are still checked:
    equals($('input[value="1"]', this.sandbox).attr('checked'), true);
});

test('mobile > ALL', function() {
    // Check ALL mobile platforms:
    this.check('input[value="9"]');
    // Check Android:
    this.check('input[value="7"]');
    // ALL mobile is no longer checked:
    equals($('input[value="9"]', this.sandbox).attr('checked'), false);
});

test('ALL > mobile', function() {
    // Check Android, Maemo:
    this.check('input[value="7"]');
    this.check('input[value="8"]');
    // Check ALL mobile platforms:
    this.check('input[value="9"]');
    // Specific platforms are no longer checked:
    equals($('input[value="7"]', this.sandbox).attr('checked'), false);
    equals($('input[value="8"]', this.sandbox).attr('checked'), false);
});


module('slugified fields', {
    setup: function() {
        this.sandbox = tests.createSandbox('#slugified-field');
    },
    teardown: function() {
        this.sandbox.remove();
    }
});

asyncTest('non-customized', function() {
    load_unicode();
    tests.waitFor(function() {
        return z == null || z.unicode_letters;
    }).thenDo(function() {
        $("#id_name").val("password exporter");
        slugify();
        equals($("#id_slug").val(), 'password-exporter');
        $("#id_name").val("password exporter2");
        slugify();
        equals($("#id_slug").val(), 'password-exporter2');
        start();
    });
});

asyncTest('customized', function() {
    load_unicode();
    tests.waitFor(function() {
        return z == null || z.unicode_letters;
    }).thenDo(function() {
        $("#id_name").val("password exporter");
        slugify();
        equals($("#id_slug").val(), 'password-exporter');
        $("#id_slug").attr("data-customized", 1);
        $("#id_name").val("password exporter2");
        slugify();
        equals($("#id_slug").val(), 'password-exporter');
        start();
    });
});


module('exclude platforms', {
    setup: function() {
        this._FormData = z.FormData;
        z.FormData = tests.StubOb(z.FormData, {
            send: function() {}
        });
        this.sandbox = tests.createSandbox('#addon-platform-exclusion');

        $.fx.off = true;

        $('#upload-file-input', this.sandbox).addonUploader();

        this.el = $('#upload-file-input', this.sandbox)[0];
        this.el.files = [{
            size: 200,
            name: 'some-addon.xpi'
        }];

        $(this.el).trigger('change');
    },
    teardown: function() {
        this.sandbox.remove();
        z.FormData = this._FormData;
    }
});

test('mobile', function() {
    var sb = this.sandbox;
    results = {
        validation: {
            "errors": 0,
            "detected_type": "mobile",
            "success": true,
            "warnings": 0,
            "notices": 0,
            "message_tree": {},
            "messages": [],
            "rejected": false
        },
        // exclude all but mobile:
        platforms_to_exclude: ['1', '2', '3', '5']
    };

    $(this.el).trigger("upload_success_results",
                       [{name: 'somefile.txt'}, results]);

    // All desktop platforms disabled:
    equals($('.desktop-platforms input:eq(0)', sb).attr('disabled'), true);
    equals($('.desktop-platforms input:eq(1)', sb).attr('disabled'), true);
    equals($('.desktop-platforms input:eq(2)', sb).attr('disabled'), true);
    equals($('.desktop-platforms input:eq(3)', sb).attr('disabled'), true);
    equals($('.desktop-platforms label:eq(0)', sb).hasClass('platform-disabled'),
           true);

    ok($('.platform ul.errorlist', sb).length > 0, 'Message shown to user');

    // All mobile platforms not disabled:
    equals($('.mobile-platforms input:eq(0)', sb).attr('disabled'), false);
    equals($('.mobile-platforms input:eq(1)', sb).attr('disabled'), false);
    equals($('.mobile-platforms input:eq(2)', sb).attr('disabled'), false);
});

test('existing platforms', function() {
    var sb = this.sandbox;
    results = {
        validation: {
            "errors": 0,
            "detected_type": "extension",
            "success": true,
            "warnings": 0,
            "notices": 0,
            "message_tree": {},
            "messages": [],
            "rejected": false
        },
        // exclude one platform as if this version already fulfilled it
        platforms_to_exclude: ['2']
    };

    $(this.el).trigger("upload_success_results",
                       [{name: 'somefile.txt'}, results]);

    equals($('.desktop-platforms input:eq(0)', sb).attr('disabled'), false);
    equals($('.desktop-platforms input:eq(1)', sb).attr('disabled'), true);
    equals($('.desktop-platforms label:eq(0)', sb).hasClass('platform-disabled'),
           false);
});


});
