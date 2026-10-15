import { PRODUCT_DESCRIPTION, PRODUCT_NAME } from '@/utils/brand';
import { StyleProvider, createCache } from '@ant-design/cssinjs';
import Document, { DocumentContext, Head, Html, Main, NextScript } from 'next/document';
import { doExtraStyle } from '../genAntdCss';

class MyDocument extends Document {
  static async getInitialProps(ctx: DocumentContext) {
    const cache = createCache();
    let fileName = '';
    const originalRenderPage = ctx.renderPage;
    ctx.renderPage = () =>
      originalRenderPage({
        enhanceApp: App => props => (
          <StyleProvider cache={cache} hashPriority='high'>
            <App {...props} />
          </StyleProvider>
        ),
      });
    const initialProps = await Document.getInitialProps(ctx);

    fileName = doExtraStyle({
      cache,
    });

    return {
      ...initialProps,
      styles: (
        <>
          {initialProps.styles}
          {/* 1.2 inject css */}
          {fileName && <link rel='stylesheet' href={`/${fileName}`} />}
        </>
      ),
    };
  }

  render() {
    return (
      <Html lang='en'>
        <Head>
          <link rel='icon' href='/favicon.ico?v=shuzhi-workshop' />
          <link rel='icon' type='image/svg+xml' href='/brand/icon.svg' />
          <meta name='description' content={PRODUCT_DESCRIPTION} />
          <meta property='og:description' content={PRODUCT_DESCRIPTION} />
          <meta property='og:title' content={PRODUCT_NAME} />
          <meta property='og:site_name' content={PRODUCT_NAME} />
        </Head>
        <body>
          <Main />
          <NextScript />
        </body>
      </Html>
    );
  }
}

export default MyDocument;
