export const name = 'dsh-dsb';
export const reusable = true;
export const inject = ['webServer', 'connection'];

// One Loader source owns the client graph row; Cordis keeps three host fibers.
export async function apply(ctx, config) {
  const component = config.component;
  if (!['dsb-brand', 'dsb-report', 'dsb-ui'].includes(component)) throw new Error('Unknown DSB component');
  try {
    const plugin = await import(`./${component}/index.js`);
    return await plugin.apply(ctx);
  } catch (error) {
    if (component === 'dsb-report') {
      // A thrown startup error would make app-boot reject the entire host tree.
      ctx.logger.warn('[dsb-report] Component initialization failed:', error);
      return;
    }
    throw error;
  }
}
