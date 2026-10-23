window.__ModuleLoader__.load({
  id: 'dsb-brand',
  factory: require => {
    const {jsx} = require('react/jsx-runtime');
    function BrandMark({size}) {
      return jsx('span', {
        style: {width: size, height: size, display: 'inline-block', borderRadius: '50%', overflow: 'hidden', flex: 'none'},
        children: jsx('img', {src: window.__DSB_BRAND_ICON__, alt: '', width: size, height: size,
          style: {display: 'block', width: '100%', height: '100%', objectFit: 'cover', transform: 'scale(1.5)'}})
      });
    }
    function BrandName() {
      return jsx('span', {style: {fontSize: 18, lineHeight: '24px', whiteSpace: 'nowrap', letterSpacing: 0}, children: '逗沙包'});
    }
    return {
      inject: ['slots'],
      apply(ctx) {
        ctx.slots.inject('sidebar.brand.mark', () => ctx.slots.inject('sidebar.brand.name', function* () {
          yield ctx.slots.register({name: 'sidebar.brand.mark'}, BrandMark);
          yield ctx.slots.register({name: 'sidebar.brand.name'}, BrandName);
        }));
      }
    };
  }
});
