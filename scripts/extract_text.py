#!/usr/bin/env python3
"""Extract readable static HTML, including image descriptions, without executing code."""
import argparse
from html.parser import HTMLParser
from pathlib import Path
import re


class TextExtractor(HTMLParser):
    BLOCKS = set('address article aside blockquote br div dl dt dd figcaption figure footer h1 h2 h3 h4 h5 h6 header hr li main nav ol p pre section table tr ul'.split())
    SKIP = {'script', 'style', 'template', 'noscript'}

    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.skip = None
        self.skip_depth = 0

    def handle_starttag(self, tag, attrs):
        if self.skip:
            if tag == self.skip:
                self.skip_depth += 1
            return
        if tag in self.SKIP:
            self.skip = tag
            self.skip_depth = 1
            return
        if tag in self.BLOCKS:
            self.parts.append('\n')
        if tag in {'td', 'th'}:
            self.parts.append(' | ')
        if tag == 'img':
            alt = dict(attrs).get('alt')
            if alt:
                self.parts.append(' [图片说明：' + alt + '] ')

    def handle_endtag(self, tag):
        if self.skip:
            if tag == self.skip:
                self.skip_depth -= 1
                if self.skip_depth == 0:
                    self.skip = None
            return
        if tag in self.BLOCKS:
            self.parts.append('\n')

    def handle_data(self, data):
        if not self.skip:
            self.parts.append(data)

    def text(self):
        lines = [re.sub(r'[\t \r\f\v]+', ' ', x).strip() for x in ''.join(self.parts).splitlines()]
        return '\n'.join(x for x in lines if x) + '\n'


def extract(source):
    parser = TextExtractor()
    parser.feed(source)
    parser.close()
    return parser.text()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('input', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    if args.input.resolve() == args.output.resolve():
        parser.error('输入和输出不能是同一个文件')
    try:
        result = extract(args.input.read_text(encoding='utf-8-sig'))
        args.output.parent.mkdir(parents=True, exist_ok=True)
        args.output.write_text(result, encoding='utf-8')
    except (OSError, UnicodeError) as error:
        parser.exit(2, '提取失败：' + type(error).__name__ + '\n')
    print('提取完成，共 %d 行。仅覆盖静态 HTML，请对照原文。' % len(result.splitlines()))


if __name__ == '__main__':
    main()
