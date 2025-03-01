import math
import chainer
from chainer import Chain
from chainer import functions as F
from chainer import links as L
from chainer import Variable


def inception_score(model, ims, batch_size=25, splits=10):
    """Compute the inception score for given images.

    Default batch_size is 100 and split size is 10. Please refer to the
    official implementation. It is recommended to to use at least 50000
    images to obtain a reliable score.

    Reference:
    https://github.com/openai/improved-gan/blob/master/inception_score/model.py

    """
    n, c, w, h = ims.shape
    n_batches = int(math.ceil(float(n) / float(batch_size)))

    xp = model.xp

    print('Batch size:', batch_size)
    print('Total number of images:', n)
    print('Total number of batches:', n_batches)

    # Compute the softmax predicitions for for all images, split into batches
    # in order to fit in memory

    ys = xp.empty((n, 1008), dtype=xp.float32)  # Softmax container

    for i in range(n_batches):

        print('Running batch', i + 1, '/', n_batches, '...')

        batch_start = (i * batch_size)
        batch_end = min((i + 1) * batch_size, n)

        ims_batch = ims[batch_start:batch_end]
        ims_batch = xp.asarray(ims_batch)  # To GPU if using CuPy
        ims_batch = Variable(ims_batch)

        # Resize image to the shape expected by the inception module
        if (w, h) != (299, 299):
            ims_batch = F.resize_images(ims_batch, (299, 299))  # bilinear

        # Feed images to the inception module to get the softmax predictions
        with chainer.using_config('train', False), chainer.using_config('enable_backprop', False):
            y = model(ims_batch)
        ys[batch_start:batch_end] = y.data

    # Compute the inception score based on the softmax predictions of the
    # inception module.
    scores = xp.empty((splits), dtype=xp.float32)  # Split inception scores
    for i in range(splits):
        part = ys[(i * n // splits):((i + 1) * n // splits), :]
        kl = part * (xp.log(part) -
                     xp.log(xp.expand_dims(xp.mean(part, 0), 0)))
        kl = xp.mean(xp.sum(kl, 1))
        scores[i] = xp.exp(kl)

    return xp.mean(scores), xp.std(scores)


class Pooling2D(object):

    def __init__(self, ksize, stride, pad):
        self.ksize = ksize
        self.stride = stride
        self.pad = pad


class AveragePooling2D(Pooling2D):

    def __call__(self, x):
        return F.average_pooling_2d(
            x, ksize=self.ksize, stride=self.stride, pad=self.pad)


class MaxPooling2D(Pooling2D):

    def __call__(self, x):
        return F.max_pooling_2d(
            x, ksize=self.ksize, stride=self.stride, pad=self.pad)


class Mixed(Chain):

    def __init__(self, trunk):
        super().__init__()
        for name, link in trunk:
            self.add_link(name, link)
        self.trunk = trunk

    def __call__(self, x):
        hs = []
        for name, _ in self.trunk:
            h = getattr(self, name)(x)
            hs.append(h)
        return F.concat(hs)


class Tower(Chain):

    def __init__(self, trunk):
        super().__init__()
        for name, link in trunk:
            if not name.startswith('_'):
                self.add_link(name, link)
        self.trunk = trunk

    def __call__(self, x):
        h = x
        for name, f in self.trunk:
            if name.startswith('_'):  # AveragePooling2D, MaxPooling2D or ReLU
                h = f(h)
            else:
                h = getattr(self, name)(h)  # Link
        return h


class Inception(Chain):

    def __init__(self):
        super(Inception, self).__init__()

        with self.init_scope():
            self.conv = L.Convolution2D(3, 32, 3, stride=2, pad=0)
            self.conv_1 = L.Convolution2D(32, 32, 3, stride=1, pad=0)
            self.conv_2 = L.Convolution2D(32, 64, 3, stride=1, pad=1)
            self.conv_3 = L.Convolution2D(64, 80, 1, stride=1, pad=0)
            self.conv_4 = L.Convolution2D(80, 192, 3, stride=1, pad=0)
            self.bn_conv = L.BatchNormalization(32)
            self.bn_conv_1 = L.BatchNormalization(32)
            self.bn_conv_2 = L.BatchNormalization(64)
            self.bn_conv_3 = L.BatchNormalization(80)
            self.bn_conv_4 = L.BatchNormalization(192)
            self.mixed = Mixed([
                ('conv', Tower([
                    ('conv', L.Convolution2D(192, 64, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(64)),
                    ('_relu', F.relu)
                ])),
                ('tower', Tower([
                    ('conv', L.Convolution2D(192, 48, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(48)),
                    ('_relu', F.relu),
                    ('conv_1', L.Convolution2D(48, 64, 5, stride=1, pad=2)),
                    ('bn_conv_1', L.BatchNormalization(64)),
                    ('_relu_1', F.relu)
                ])),
                ('tower_1', Tower([
                    ('conv', L.Convolution2D(192, 64, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(64)),
                    ('_relu', F.relu),
                    ('conv_1', L.Convolution2D(64, 96, 3, stride=1, pad=1)),
                    ('bn_conv_1', L.BatchNormalization(96)),
                    ('_relu_1', F.relu),
                    ('conv_2', L.Convolution2D(96, 96, 3, stride=1, pad=1)),
                    ('bn_conv_2', L.BatchNormalization(96)),
                    ('_relu_2', F.relu)
                ])),
                ('tower_2', Tower([
                    ('_pooling', AveragePooling2D(3, 1, 1)),
                    ('conv', L.Convolution2D(192, 32, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(32)),
                    ('_relu', F.relu)
                ]))
            ])
            self.mixed_1 = Mixed([
                ('conv', Tower([
                    ('conv', L.Convolution2D(256, 64, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(64)),
                    ('_relu', F.relu)
                ])),
                ('tower', Tower([
                    ('conv', L.Convolution2D(256, 48, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(48)),
                    ('_relu', F.relu),
                    ('conv_1', L.Convolution2D(48, 64, 5, stride=1, pad=2)),
                    ('bn_conv_1', L.BatchNormalization(64)),
                    ('_relu_1', F.relu)
                ])),
                ('tower_1', Tower([
                    ('conv', L.Convolution2D(256, 64, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(64)),
                    ('_relu', F.relu),
                    ('conv_1', L.Convolution2D(64, 96, 3, stride=1, pad=1)),
                    ('bn_conv_1', L.BatchNormalization(96)),
                    ('_relu_1', F.relu),
                    ('conv_2', L.Convolution2D(96, 96, 3, stride=1, pad=1)),
                    ('bn_conv_2', L.BatchNormalization(96)),
                    ('_relu_2', F.relu)
                ])),
                ('tower_2', Tower([
                    ('_pooling', AveragePooling2D(3, 1, 1)),
                    ('conv', L.Convolution2D(256, 64, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(64)),
                    ('_relu', F.relu)
                ]))
            ])
            self.mixed_2 = Mixed([
                ('conv', Tower([
                    ('conv', L.Convolution2D(288, 64, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(64)),
                    ('_relu', F.relu)
                ])),
                ('tower', Tower([
                    ('conv', L.Convolution2D(288, 48, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(48)),
                    ('_relu', F.relu),
                    ('conv_1', L.Convolution2D(48, 64, 5, stride=1, pad=2)),
                    ('bn_conv_1', L.BatchNormalization(64)),
                    ('_relu_1', F.relu)
                ])),
                ('tower_1', Tower([
                    ('conv', L.Convolution2D(288, 64, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(64)),
                    ('_relu', F.relu),
                    ('conv_1', L.Convolution2D(64, 96, 3, stride=1, pad=1)),
                    ('bn_conv_1', L.BatchNormalization(96)),
                    ('_relu_1', F.relu),
                    ('conv_2', L.Convolution2D(96, 96, 3, stride=1, pad=1)),
                    ('bn_conv_2', L.BatchNormalization(96)),
                    ('_relu_2', F.relu)
                ])),
                ('tower_2', Tower([
                    ('_pooling', AveragePooling2D(3, 1, 1)),
                    ('conv', L.Convolution2D(288, 64, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(64)),
                    ('_relu', F.relu)
                ]))
            ])
            self.mixed_3 = Mixed([
                ('conv', Tower([
                    ('conv', L.Convolution2D(288, 384, 3, stride=2, pad=0)),
                    ('bn_conv', L.BatchNormalization(384)),
                    ('_relu', F.relu)
                ])),
                ('tower', Tower([
                    ('conv', L.Convolution2D(288, 64, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(64)),
                    ('_relu', F.relu),
                    ('conv_1', L.Convolution2D(64, 96, 3, stride=1, pad=1)),
                    ('bn_conv_1', L.BatchNormalization(96)),
                    ('_relu_1', F.relu),
                    ('conv_2', L.Convolution2D(96, 96, 3, stride=2, pad=0)),
                    ('bn_conv_2', L.BatchNormalization(96)),
                    ('_relu_2', F.relu)
                ])),
                ('pool', Tower([
                    ('_pooling', MaxPooling2D(3, 2, 0))
                ]))
            ])
            self.mixed_4 = Mixed([
                ('conv', Tower([
                    ('conv', L.Convolution2D(768, 192, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(192)),
                    ('_relu', F.relu)
                ])),
                ('tower', Tower([
                    ('conv', L.Convolution2D(768, 128, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(128)),
                    ('_relu', F.relu),
                    ('conv_1', L.Convolution2D(
                        128, 128, (1, 7), stride=1, pad=(0, 3))),
                    ('bn_conv_1', L.BatchNormalization(128)),
                    ('_relu_1', F.relu),
                    ('conv_2', L.Convolution2D(
                        128, 192, (7, 1), stride=1, pad=(3, 0))),
                    ('bn_conv_2', L.BatchNormalization(192)),
                    ('_relu_2', F.relu)
                ])),
                ('tower_1', Tower([
                    ('conv', L.Convolution2D(768, 128, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(128)),
                    ('_relu', F.relu),
                    ('conv_1', L.Convolution2D(
                        128, 128, (7, 1), stride=1, pad=(3, 0))),
                    ('bn_conv_1', L.BatchNormalization(128)),
                    ('_relu_1', F.relu),
                    ('conv_2', L.Convolution2D(
                        128, 128, (1, 7), stride=1, pad=(0, 3))),
                    ('bn_conv_2', L.BatchNormalization(128)),
                    ('_relu_2', F.relu),
                    ('conv_3', L.Convolution2D(
                        128, 128, (7, 1), stride=1, pad=(3, 0))),
                    ('bn_conv_3', L.BatchNormalization(128)),
                    ('_relu_3', F.relu),
                    ('conv_4', L.Convolution2D(
                        128, 192, (1, 7), stride=1, pad=(0, 3))),
                    ('bn_conv_4', L.BatchNormalization(192)),
                    ('_relu_4', F.relu)
                ])),
                ('tower_2', Tower([
                    ('_pooling', AveragePooling2D(3, 1, 1)),
                    ('conv', L.Convolution2D(768, 192, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(192)),
                    ('_relu', F.relu)
                ]))
            ])
            self.mixed_5 = Mixed([
                ('conv', Tower([
                    ('conv', L.Convolution2D(768, 192, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(192)),
                    ('_relu', F.relu)
                ])),
                ('tower', Tower([
                    ('conv', L.Convolution2D(768, 160, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(160)),
                    ('_relu', F.relu),
                    ('conv_1', L.Convolution2D(
                        160, 160, (1, 7), stride=1, pad=(0, 3))),
                    ('bn_conv_1', L.BatchNormalization(160)),
                    ('_relu_1', F.relu),
                    ('conv_2', L.Convolution2D(
                        160, 192, (7, 1), stride=1, pad=(3, 0))),
                    ('bn_conv_2', L.BatchNormalization(192)),
                    ('_relu_2', F.relu)
                ])),
                ('tower_1', Tower([
                    ('conv', L.Convolution2D(768, 160, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(160)),
                    ('_relu', F.relu),
                    ('conv_1', L.Convolution2D(
                        160, 160, (7, 1), stride=1, pad=(3, 0))),
                    ('bn_conv_1', L.BatchNormalization(160)),
                    ('_relu_1', F.relu),
                    ('conv_2', L.Convolution2D(
                        160, 160, (1, 7), stride=1, pad=(0, 3))),
                    ('bn_conv_2', L.BatchNormalization(160)),
                    ('_relu_2', F.relu),
                    ('conv_3', L.Convolution2D(
                        160, 160, (7, 1), stride=1, pad=(3, 0))),
                    ('bn_conv_3', L.BatchNormalization(160)),
                    ('_relu_3', F.relu),
                    ('conv_4', L.Convolution2D(
                        160, 192, (1, 7), stride=1, pad=(0, 3))),
                    ('bn_conv_4', L.BatchNormalization(192)),
                    ('_relu_4', F.relu)
                ])),
                ('tower_2', Tower([
                    ('_pooling', AveragePooling2D(3, 1, 1)),
                    ('conv', L.Convolution2D(768, 192, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(192)),
                    ('_relu', F.relu)
                ]))
            ])
            self.mixed_6 = Mixed([
                ('conv', Tower([
                    ('conv', L.Convolution2D(768, 192, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(192)),
                    ('_relu', F.relu)
                ])),
                ('tower', Tower([
                    ('conv', L.Convolution2D(768, 160, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(160)),
                    ('_relu', F.relu),
                    ('conv_1', L.Convolution2D(
                        160, 160, (1, 7), stride=1, pad=(0, 3))),
                    ('bn_conv_1', L.BatchNormalization(160)),
                    ('_relu_1', F.relu),
                    ('conv_2', L.Convolution2D(
                        160, 192, (7, 1), stride=1, pad=(3, 0))),
                    ('bn_conv_2', L.BatchNormalization(192)),
                    ('_relu_2', F.relu)
                ])),
                ('tower_1', Tower([
                    ('conv', L.Convolution2D(768, 160, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(160)),
                    ('_relu', F.relu),
                    ('conv_1', L.Convolution2D(
                        160, 160, (7, 1), stride=1, pad=(3, 0))),
                    ('bn_conv_1', L.BatchNormalization(160)),
                    ('_relu_1', F.relu),
                    ('conv_2', L.Convolution2D(
                        160, 160, (1, 7), stride=1, pad=(0, 3))),
                    ('bn_conv_2', L.BatchNormalization(160)),
                    ('_relu_2', F.relu),
                    ('conv_3', L.Convolution2D(
                        160, 160, (7, 1), stride=1, pad=(3, 0))),
                    ('bn_conv_3', L.BatchNormalization(160)),
                    ('_relu_3', F.relu),
                    ('conv_4', L.Convolution2D(
                        160, 192, (1, 7), stride=1, pad=(0, 3))),
                    ('bn_conv_4', L.BatchNormalization(192)),
                    ('_relu_4', F.relu)
                ])),
                ('tower_2', Tower([
                    ('_pooling', AveragePooling2D(3, 1, 1)),
                    ('conv', L.Convolution2D(768, 192, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(192)),
                    ('_relu', F.relu)
                ]))
            ])
            self.mixed_7 = Mixed([
                ('conv', Tower([
                    ('conv', L.Convolution2D(768, 192, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(192)),
                    ('_relu', F.relu)
                ])),
                ('tower', Tower([
                    ('conv', L.Convolution2D(768, 192, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(192)),
                    ('_relu', F.relu),
                    ('conv_1', L.Convolution2D(
                        192, 192, (1, 7), stride=1, pad=(0, 3))),
                    ('bn_conv_1', L.BatchNormalization(192)),
                    ('_relu_1', F.relu),
                    ('conv_2', L.Convolution2D(
                        192, 192, (7, 1), stride=1, pad=(3, 0))),
                    ('bn_conv_2', L.BatchNormalization(192)),
                    ('_relu_2', F.relu)
                ])),
                ('tower_1', Tower([
                    ('conv', L.Convolution2D(768, 192, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(192)),
                    ('_relu', F.relu),
                    ('conv_1', L.Convolution2D(
                        192, 192, (7, 1), stride=1, pad=(3, 0))),
                    ('bn_conv_1', L.BatchNormalization(192)),
                    ('_relu_1', F.relu),
                    ('conv_2', L.Convolution2D(
                        192, 192, (1, 7), stride=1, pad=(0, 3))),
                    ('bn_conv_2', L.BatchNormalization(192)),
                    ('_relu_2', F.relu),
                    ('conv_3', L.Convolution2D(
                        192, 192, (7, 1), stride=1, pad=(3, 0))),
                    ('bn_conv_3', L.BatchNormalization(192)),
                    ('_relu_3', F.relu),
                    ('conv_4', L.Convolution2D(
                        192, 192, (1, 7), stride=1, pad=(0, 3))),
                    ('bn_conv_4', L.BatchNormalization(192)),
                    ('_relu_4', F.relu)
                ])),
                ('tower_2', Tower([
                    ('_pooling', AveragePooling2D(3, 1, 1)),
                    ('conv', L.Convolution2D(768, 192, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(192)),
                    ('_relu', F.relu)
                ]))
            ])
            self.mixed_8 = Mixed([
                ('tower', Tower([
                    ('conv', L.Convolution2D(768, 192, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(192)),
                    ('_relu', F.relu),
                    ('conv_1', L.Convolution2D(192, 320, 3, stride=2, pad=0)),
                    ('bn_conv_1', L.BatchNormalization(320)),
                    ('_relu_1', F.relu)
                ])),
                ('tower_1', Tower([
                    ('conv', L.Convolution2D(768, 192, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(192)),
                    ('_relu', F.relu),
                    ('conv_1', L.Convolution2D(
                        192, 192, (1, 7), stride=1, pad=(0, 3))),
                    ('bn_conv_1', L.BatchNormalization(192)),
                    ('_relu_1', F.relu),
                    ('conv_2', L.Convolution2D(
                        192, 192, (7, 1), stride=1, pad=(3, 0))),
                    ('bn_conv_2', L.BatchNormalization(192)),
                    ('_relu_2', F.relu),
                    ('conv_3', L.Convolution2D(192, 192, 3, stride=2, pad=0)),
                    ('bn_conv_3', L.BatchNormalization(192)),
                    ('_relu_3', F.relu)
                ])),
                ('pool', Tower([
                    ('_pooling', MaxPooling2D(3, 2, 0))
                ]))
            ])
            self.mixed_9 = Mixed([
                ('conv', Tower([
                    ('conv', L.Convolution2D(1280, 320, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(320)),
                    ('_relu', F.relu),
                ])),
                ('tower', Tower([
                    ('conv', L.Convolution2D(1280, 384, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(384)),
                    ('_relu', F.relu),
                    ('mixed', Mixed([
                        ('conv', Tower([
                            ('conv', L.Convolution2D(
                                384, 384, (1, 3), stride=1, pad=(0, 1))),
                            ('bn_conv', L.BatchNormalization(384)),
                            ('_relu', F.relu),
                        ])),
                        ('conv_1', Tower([
                            ('conv_1', L.Convolution2D(
                                384, 384, (3, 1), stride=1, pad=(1, 0))),
                            ('bn_conv_1', L.BatchNormalization(384)),
                            ('_relu_1', F.relu),
                        ]))
                    ]))
                ])),
                ('tower_1', Tower([
                    ('conv', L.Convolution2D(1280, 448, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(448)),
                    ('_relu', F.relu),
                    ('conv_1', L.Convolution2D(448, 384, 3, stride=1, pad=1)),
                    ('bn_conv_1', L.BatchNormalization(384)),
                    ('_relu_1', F.relu),
                    ('mixed', Mixed([
                        ('conv', Tower([
                            ('conv', L.Convolution2D(
                                384, 384, (1, 3), stride=1, pad=(0, 1))),
                            ('bn_conv', L.BatchNormalization(384)),
                            ('_relu', F.relu),
                        ])),
                        ('conv_1', Tower([
                            ('conv_1', L.Convolution2D(
                                384, 384, (3, 1), stride=1, pad=(1, 0))),
                            ('bn_conv_1', L.BatchNormalization(384)),
                            ('_relu_1', F.relu),
                        ]))
                    ]))
                ])),
                ('tower_2', Tower([
                    ('_pooling', AveragePooling2D(3, 1, 1)),
                    ('conv', L.Convolution2D(1280, 192, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(192)),
                    ('_relu', F.relu)
                ]))
            ])
            self.mixed_10 = Mixed([
                ('conv', Tower([
                    ('conv', L.Convolution2D(2048, 320, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(320)),
                    ('_relu', F.relu),
                ])),
                ('tower', Tower([
                    ('conv', L.Convolution2D(2048, 384, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(384)),
                    ('_relu', F.relu),
                    ('mixed', Mixed([
                        ('conv', Tower([
                            ('conv', L.Convolution2D(
                                384, 384, (1, 3), stride=1, pad=(0, 1))),
                            ('bn_conv', L.BatchNormalization(384)),
                            ('_relu', F.relu),
                        ])),
                        ('conv_1', Tower([
                            ('conv_1', L.Convolution2D(
                                384, 384, (3, 1), stride=1, pad=(1, 0))),
                            ('bn_conv_1', L.BatchNormalization(384)),
                            ('_relu_1', F.relu),
                        ]))
                    ]))
                ])),
                ('tower_1', Tower([
                    ('conv', L.Convolution2D(2048, 448, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(448)),
                    ('_relu', F.relu),
                    ('conv_1', L.Convolution2D(448, 384, 3, stride=1, pad=1)),
                    ('bn_conv_1', L.BatchNormalization(384)),
                    ('_relu_1', F.relu),
                    ('mixed', Mixed([
                        ('conv', Tower([
                            ('conv', L.Convolution2D(
                                384, 384, (1, 3), stride=1, pad=(0, 1))),
                            ('bn_conv', L.BatchNormalization(384)),
                            ('_relu', F.relu)
                        ])),
                        ('conv_1', Tower([
                            ('conv_1', L.Convolution2D(
                                384, 384, (3, 1), stride=1, pad=(1, 0))),
                            ('bn_conv_1', L.BatchNormalization(384)),
                            ('_relu_1', F.relu)
                        ]))
                    ]))
                ])),
                ('tower_2', Tower([
                    ('_pooling', MaxPooling2D(3, 1, 1)),
                    ('conv', L.Convolution2D(2048, 192, 1, stride=1, pad=0)),
                    ('bn_conv', L.BatchNormalization(192)),
                    ('_relu', F.relu)
                ]))
            ])
            self.logit = L.Linear(2048, 1008)

    def __call__(self, x, get_feature=False):
        """Input dims are (batch_size, 3, 299, 299)."""

        # assert x.shape[1:] == (3, 299, 299)

        x -= 128.0
        x *= 0.0078125

        h = F.relu(self.bn_conv(self.conv(x)))
        # assert h.shape[1:] == (32, 149, 149)

        h = F.relu(self.bn_conv_1(self.conv_1(h)))
        # assert h.shape[1:] == (32, 147, 147)

        h = F.relu(self.bn_conv_2(self.conv_2(h)))
        # assert h.shape[1:] == (64, 147, 147)

        h = F.max_pooling_2d(h, 3, stride=2, pad=0)
        # assert h.shape[1:] == (64, 73, 73)

        h = F.relu(self.bn_conv_3(self.conv_3(h)))
        # assert h.shape[1:] == (80, 73, 73)

        h = F.relu(self.bn_conv_4(self.conv_4(h)))
        # assert h.shape[1:] == (192, 71, 71)

        h = F.max_pooling_2d(h, 3, stride=2, pad=0)
        # assert h.shape[1:] == (192, 35, 35)

        h = self.mixed(h)
        # assert h.shape[1:] == (256, 35, 35)

        h = self.mixed_1(h)
        # assert h.shape[1:] == (288, 35, 35)

        h = self.mixed_2(h)
        # assert h.shape[1:] == (288, 35, 35)

        h = self.mixed_3(h)
        # assert h.shape[1:] == (768, 17, 17)

        h = self.mixed_4(h)
        # assert h.shape[1:] == (768, 17, 17)

        h = self.mixed_5(h)
        # assert h.shape[1:] == (768, 17, 17)

        h = self.mixed_6(h)
        # assert h.shape[1:] == (768, 17, 17)

        h = self.mixed_7(h)
        # assert h.shape[1:] == (768, 17, 17)

        h = self.mixed_8(h)
        # assert h.shape[1:] == (1280, 8, 8)

        h = self.mixed_9(h)
        # assert h.shape[1:] == (2048, 8, 8)

        h = self.mixed_10(h)
        # assert h.shape[1:] == (2048, 8, 8)

        h = F.average_pooling_2d(h, 8, 1)
        # assert h.shape[1:] == (2048, 1, 1)

        h = F.reshape(h, (-1, 2048))
        if get_feature:
            return h
        h = self.logit(h)
        h = F.softmax(h)

        # assert h.shape[1:] == (1008,)

        return h
