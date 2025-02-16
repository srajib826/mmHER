import sys
import os
import pickle
import struct
import time
import numpy as np
import array as arr
import configuration as cfg
from scipy.ndimage import convolve1d
import matplotlib.pyplot as plt


def read8byte(x):
    return struct.unpack('<hhhh', x)


class FrameConfig:  #
    def __init__(self):
        #  configs in configuration.py
        self.numTxAntennas = cfg.NUM_TX
        self.numRxAntennas = cfg.NUM_RX
        self.numLoopsPerFrame = cfg.LOOPS_PER_FRAME
        self.numADCSamples = cfg.ADC_SAMPLES
        self.numAngleBins = cfg.NUM_ANGLE_BINS

        self.numChirpsPerFrame = self.numTxAntennas * self.numLoopsPerFrame
        self.numRangeBins = self.numADCSamples
        self.numDopplerBins = self.numLoopsPerFrame

        # calculate size of one chirp in short.
        self.chirpSize = self.numRxAntennas * self.numADCSamples
        # calculate size of one chirp loop in short. 3Tx has three chirps in one loop for TDM.
        self.chirpLoopSize = self.chirpSize * self.numTxAntennas
        # calculate size of one frame in short.
        self.frameSize = self.chirpLoopSize * self.numLoopsPerFrame


class PointCloudProcessCFG:  #
    def __init__(self):
        self.frameConfig = FrameConfig()
        self.enableStaticClutterRemoval = True
        self.EnergyTop128 = True
        self.RangeCut = False
        self.outputVelocity = True
        self.outputSNR = True
        self.outputRange = True
        self.outputInMeter = True
        self.EnergyThrMed = True
        self.ConstNoPCD = False
        self.dopplerToLog = False

        # 0,1,2 for x,y,z
        dim = 3
        if self.outputVelocity:
            self.velocityDim = dim
            dim += 1
        if self.outputSNR:
            self.SNRDim = dim
            dim += 1
        if self.outputRange:
            self.rangeDim = dim
            dim += 1
        self.couplingSignatureBinFrontIdx = 5
        self.couplingSignatureBinRearIdx = 4
        self.sumCouplingSignatureArray = np.zeros((self.frameConfig.numTxAntennas, self.frameConfig.numRxAntennas,
                                                   self.couplingSignatureBinFrontIdx + self.couplingSignatureBinRearIdx),
                                                  dtype=np.complex128)


class RawDataReader:
    def __init__(self, path):
        self.path = path
        self.ADCBinFile = open(path, 'rb')

    def getNextFrame(self, frameconfig):
        frame = np.frombuffer(self.ADCBinFile.read(frameconfig.frameSize * 4), dtype=np.int16)
        return frame

    def close(self):
        self.ADCBinFile.close()


def bin2np_frame(bin_frame):  #
    np_frame = np.zeros(shape=(len(bin_frame) // 2), dtype=np.complex_)
    np_frame[0::2] = bin_frame[0::4] + 1j * bin_frame[2::4]
    np_frame[1::2] = bin_frame[1::4] + 1j * bin_frame[3::4]
    return np_frame


def frameReshape(frame, frameConfig):  #
    frameWithChirp = np.reshape(frame, (
    frameConfig.numLoopsPerFrame, frameConfig.numTxAntennas, frameConfig.numRxAntennas, -1))
    return frameWithChirp.transpose(1, 2, 0, 3)


def rangeFFT(reshapedFrame, frameConfig):  #
    windowedBins1D = reshapedFrame
    rangeFFTResult = np.fft.fft(windowedBins1D)
    return rangeFFTResult


def clutter_removal(input_val, axis=0):  #
    # Reorder the axes
    reordering = np.arange(len(input_val.shape))
    reordering[0] = axis
    reordering[axis] = 0
    input_val = input_val.transpose(reordering)
    # Apply static clutter removal
    mean = input_val.mean(0)
    output_val = input_val - mean
    return output_val.transpose(reordering)


def dopplerFFT(rangeResult, frameConfig):  #
    windowedBins2D = rangeResult * np.reshape(np.hamming(frameConfig.numLoopsPerFrame), (1, 1, -1, 1))
    dopplerFFTResult = np.fft.fft(windowedBins2D, axis=2)
    dopplerFFTResult = np.fft.fftshift(dopplerFFTResult, axes=2)
    return dopplerFFTResult


def naive_xyz(virtual_ant, num_tx=3, num_rx=4, fft_size=64):  #
    assert num_tx > 2, "need a config for more than 2 TXs"
    num_detected_obj = virtual_ant.shape[1]
    azimuth_ant = virtual_ant[:2 * num_rx, :]
    azimuth_ant_padded = np.zeros(shape=(fft_size, num_detected_obj), dtype=np.complex_)
    azimuth_ant_padded[:2 * num_rx, :] = azimuth_ant

    # Process azimuth information
    azimuth_fft = np.fft.fft(azimuth_ant_padded, axis=0)
    k_max = np.argmax(np.abs(azimuth_fft), axis=0)
    peak_1 = np.zeros_like(k_max, dtype=np.complex_)
    for i in range(len(k_max)):
        peak_1[i] = azimuth_fft[k_max[i], i]

    k_max[k_max > (fft_size // 2) - 1] = k_max[k_max > (fft_size // 2) - 1] - fft_size
    wx = 2 * np.pi / fft_size * k_max
    x_vector = wx / np.pi

    # Zero pad elevation
    elevation_ant = virtual_ant[2 * num_rx:, :]
    elevation_ant_padded = np.zeros(shape=(fft_size, num_detected_obj), dtype=np.complex_)
    elevation_ant_padded[:num_rx, :] = elevation_ant

    # Process elevation information
    elevation_fft = np.fft.fft(elevation_ant, axis=0)
    elevation_max = np.argmax(np.log2(np.abs(elevation_fft)), axis=0)  # shape = (num_detected_obj, )
    peak_2 = np.zeros_like(elevation_max, dtype=np.complex_)
    for i in range(len(elevation_max)):
        peak_2[i] = elevation_fft[elevation_max[i], i]

    # Calculate elevation phase shift
    wz = np.angle(peak_1 * peak_2.conj() * np.exp(1j * 2 * wx))
    z_vector = wz / np.pi
    ypossible = 1 - x_vector ** 2 - z_vector ** 2
    y_vector = ypossible
    x_vector[ypossible < 0] = 0
    z_vector[ypossible < 0] = 0
    y_vector[ypossible < 0] = 0
    y_vector = np.sqrt(y_vector)
    return x_vector, y_vector, z_vector


def frame2pointcloud(dopplerResult, pointCloudProcessCFG):
    dopplerResultSumAllAntenna = np.sum(dopplerResult, axis=(0, 1))
    if pointCloudProcessCFG.dopplerToLog:
        dopplerResultInDB = np.log10(np.absolute(dopplerResultSumAllAntenna))
    else:
        dopplerResultInDB = np.absolute(dopplerResultSumAllAntenna)

    if pointCloudProcessCFG.RangeCut:  # filter out the bins which are too close or too far from radar
        dopplerResultInDB[:, :25] = -100
        dopplerResultInDB[:, 125:] = -100

    cfarResult = np.zeros(dopplerResultInDB.shape, bool)
    if pointCloudProcessCFG.EnergyTop128:
        top_size = 128
        energyThre128 = np.partition(dopplerResultInDB.ravel(), 128 * 256 - top_size - 1)[128 * 256 - top_size - 1]
        cfarResult[dopplerResultInDB > energyThre128] = True

    det_peaks_indices = np.argwhere(cfarResult == True)
    R = det_peaks_indices[:, 1].astype(np.float64)
    V = (det_peaks_indices[:, 0] - FrameConfig().numDopplerBins // 2).astype(np.float64)
    if pointCloudProcessCFG.outputInMeter:
        R *= cfg.RANGE_RESOLUTION
        V *= cfg.DOPPLER_RESOLUTION
    energy = dopplerResultInDB[cfarResult == True]

    AOAInput = dopplerResult[:, :, cfarResult == True]
    AOAInput = AOAInput.reshape(12, -1)

    if AOAInput.shape[1] == 0:
        return np.array([]).reshape(6, 0)
    x_vec, y_vec, z_vec = naive_xyz(AOAInput)

    x, y, z = x_vec * R, y_vec * R, z_vec * R
    pointCloud = np.concatenate((x, y, z, V, energy, R))
    pointCloud = np.reshape(pointCloud, (6, -1))
    pointCloud = pointCloud[:, y_vec != 0]
    pointCloud = np.transpose(pointCloud, (1, 0))

    if pointCloudProcessCFG.EnergyThrMed:
        idx = np.argwhere(pointCloud[:, 4] > np.median(pointCloud[:, 4])).flatten()
        pointCloud = pointCloud[idx]

    if pointCloudProcessCFG.ConstNoPCD:
        pointCloud = reg_data(pointCloud,
                              128)  # if the points number is greater than 128, just randomly sample 128 points; if the points number is less than 128, randomly duplicate some points

    return pointCloud


def reg_data(data, pc_size):  #
    pc_tmp = np.zeros((pc_size, 6), dtype=np.float32)
    pc_no = data.shape[0]
    if pc_no < pc_size:
        fill_list = np.random.choice(pc_size, size=pc_no, replace=False)
        fill_set = set(fill_list)
        pc_tmp[fill_list] = data
        dupl_list = [x for x in range(pc_size) if x not in fill_set]
        dupl_pc = np.random.choice(pc_no, size=len(dupl_list), replace=True)
        pc_tmp[dupl_list] = data[dupl_pc]
    else:
        pc_list = np.random.choice(pc_no, size=pc_size, replace=False)
        pc_tmp = data[pc_list]
    return pc_tmp


if __name__ == '__main__':
    raw_poincloud_data_for_plot = []
    bin_filename = sys.argv[1]
    # if len(sys.argv) > 2:
    #     total_frame_number = int(sys.argv[2])
    # else:
    total_frame_number =int(os.path.getsize(bin_filename)/(4*4*3*256*128))
    pointCloudProcessCFG = PointCloudProcessCFG()
    shift_arr = cfg.MMWAVE_RADAR_LOC
    bin_reader = RawDataReader(bin_filename)
    frame_no = 0
    pointcloud_list=[]
    rangresult_list =[]
    dopplerresult_list=[]
    for frame_no in range(total_frame_number):
        bin_frame = bin_reader.getNextFrame(pointCloudProcessCFG.frameConfig)
        np_frame = bin2np_frame(bin_frame)
        frameConfig = pointCloudProcessCFG.frameConfig
        reshapedFrame = frameReshape(np_frame, frameConfig)
        rangeResult = rangeFFT(reshapedFrame, frameConfig)
        rangresult_list.append(rangeResult)
        if pointCloudProcessCFG.enableStaticClutterRemoval:
            rangeResult = clutter_removal(rangeResult, axis=2)

        dopplerResult = dopplerFFT(rangeResult, frameConfig)
        dopplerresult_list.append(dopplerResult)

        pointCloud = frame2pointcloud(dopplerResult, pointCloudProcessCFG)
        pointcloud_list.append(pointCloud)
        frame_no += 1
        #print('Frame %d:' % (frame_no), pointCloud.shape)
        raw_poincloud_data_for_plot.append(pointCloud)
        
   
    

    result = {"pointCloud": pointcloud_list, "rangeResult": rangresult_list, "doppelerResult": dopplerresult_list}
    #print(result)
    fileobj = open('prd_data2.pkl','wb')
    pickle.dump(result,fileobj)
    fileobj.close()








    # fileObj = open('prd_data.pkl', 'rb')
    # exampleObj = pickle.load(fileObj)
    # fileObj.close()
    #print(type(exampleObj))
    #print(exampleObj)
    
    bin_reader.close()
     
