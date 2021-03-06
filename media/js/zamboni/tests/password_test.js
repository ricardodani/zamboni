$(document).ready(function(){
    var password = {
        setup: function() {
            this.sandbox = tests.createSandbox('#password-strength');
            this.$node = this.sandbox.find('input[type=password]');
            initStrength(this.$node);
        },
        teardown: function() {
            this.sandbox.remove();
        }
    };

    module('Passwords', password);

    test('Length', function() {
        this.$node.val('123457890').trigger('blur');
        equal(this.$node.parent().find('ul.errorlist li:hidden').exists(), true);

        this.$node.val('123').trigger('blur');
        equal(this.$node.parent().find('ul.errorlist li:hidden').exists(), false);
    });

    test('Complexity', function() {
        var $strength = this.$node.parent().find('ul.errorlist li.strength');

        this.$node.val('123').trigger('blur');
        equal($strength.hasClass('password-weak'), true);

        this.$node.val('123abcDEF').trigger('blur');
        equal($strength.hasClass('password-medium'), true);

        this.$node.val('АзәрбајҹанA13').trigger('blur');
        equal($strength.hasClass('password-strong'), true);
    });
});
